import asyncio
import collections
import datetime
import heapq
import json
import logging
import time

from .misc import maybe_awaitable

log = logging.getLogger(__name__)


class _Entry(collections.namedtuple('_Entry', 'time event args kwargs created id')):
    __slots__ = ()

    def __new__(cls, time, event, args=None, kwargs=None, created=None, id=None):
        created = created or datetime.datetime.utcnow()
        args = args or ()
        kwargs = kwargs or {}
        return super().__new__(cls, time, event, args, kwargs, created, id)

    @classmethod
    def from_record(cls, record):
        """Returns a database from a record. This is purely internal."""
        args_kwargs = record['args_kwargs']
        return cls(
            time=record['expires'],
            event=record['event'],
            args=args_kwargs['args'],
            kwargs=args_kwargs['kwargs'],
            created=record['created'],
            id=record['id'],
        )

    @property
    def utc(self):
        t = self.time
        if isinstance(t, datetime.datetime):
            return t
        return datetime.datetime.utcfromtimestamp(t)

    @property
    def seconds(self):
        delta = self.time - self.created
        if isinstance(delta, datetime.timedelta):
            delta = delta.total_seconds()

        return delta

    @property
    def short(self):
        """Returns True if the event is "short".

        A short event gives an optimization opportunity, it doesn't have to be
        sorted, in the queue or database. Instead, a Task can be created where
        it sleeps for a period of time before being dispatched.
        """
        return self.seconds <= 30


class BaseScheduler:
    """Manages timing related things.

    Unlike the scheduler in sched.py, this is designed for coroutines.
    This is why most of the public methods (adding and removing entries) are
    coroutines and must be awaited (e.g. await scheduler.add(*stuff)).

    This was made due to issues with asyncio.sleep. Naively sleeping for timing
    will not work, as asyncio.sleep can only go up to 48 days reliably. (Depending
    on the selector used it can go as far to 2 ** 64 - 1, but the minimum is
    4194303 seconds, or 2 ** 22 - 1, or ~48 days).

    PS. I don't claim credit for this.
    """
    MAX_SLEEP_TIME = 60 * 60 * 24
    SHORT_TASK_DURATION = 30

    def __init__(self, *, loop=None, timefunc=time.monotonic):
        self.time_function = timefunc
        self._loop = loop or asyncio.get_event_loop()
        self._lock = asyncio.Lock()
        self._current = None
        self._runner = None
        self._callbacks = []

    def __del__(self):
        self.close()

    @staticmethod
    def _calculate_delta(time1, time2):
        return time1 - time2

    # These four methods must be implemented in subclasses

    async def _get(self):
        raise NotImplementedError

    async def _put(self, entry):
        raise NotImplementedError

    async def _remove(self, entry):
        raise NotImplementedError

    async def _cleanup(self):
        pass

    async def _update(self):
        while True:
            self._current = timer = await self._get()
            now = self.time_function()
            delta = self._calculate_delta(timer.time, now)
            log.debug('sleeping for %s seconds', delta)

            while delta > 0:
                await asyncio.sleep(min(self.MAX_SLEEP_TIME, delta))
                delta -= self.MAX_SLEEP_TIME

            log.debug('entry %r is done, dispatching now.', timer)
            await self._dispatch(self._current)

    def _restart(self):
        self._runner.cancel()
        self._runner = self._loop.create_task(self._update())

    async def _short_task_optimization(self, delta, event):
        # XXX: Is it a good idea to use self._loop.call_later? It's short enough,
        #      and self._dispatch is not a coroutine.
        await asyncio.sleep(delta)
        self._dispatch(event)

    async def add_abs(self, when, action, args=(), kwargs=None, id=None):
        """Enter a new event in the queue at an absolute time.

        Returns an ID for the event which can be used to remove it,
        if necessary.
        """

        kwargs = kwargs or {}
        event = _Entry(when, action, args, kwargs, None)
        if event.short:
            # Allow for short timer optimization
            self._loop.create_task(self._short_task_optimization(event.seconds, event))
            return

        await self._put(event)

        if self._current and event.time <= self._current.time:
            self._restart()

    async def add(self, delay, action, args=(), kwargs=None, id=None):
        """A variant that specifies the time as a relative time.

        This is actually the more commonly used interface.
        """

        time = self.time_function() + delay
        return await self.add_abs(time, action, args, kwargs, id)

    async def remove(self, entry):
        """Removes an entry from the queue."""
        await self._remove(entry)
        self._restart()

    # Callback-related things
    async def _dispatch(self, timer):
        for cb in self._callbacks:
            try:
                await maybe_awaitable(cb, timer)
            except Exception as e:
                log.error('Callback %r raised %r', cb, e)
                raise
        log.debug('All callbacks for %r have been called successfully', timer)

    def add_callback(self, callback):
        self._callbacks.append(callback)

    def remove_callback(self, callback):
        callbacks = [cb for cb in self._callbacks if cb != callback]
        callbacks_removed = len(self._callbacks) - len(callbacks)
        self._callbacks[:] = callbacks
        return callbacks_removed

    def run(self):
        """Runs the scheduler.

        If the scheduler is already running, this does nothing.
        """

        if self.is_running():
            return

        self._runner = self._loop.create_task(self._update())

    def is_running(self):
        """Returns True if the scheduler is currenly running, False otherwise."""
        runner = self._runner
        return runner and not runner.done()

    def stop(self):
        """Stops the scheduler.

        This doesn't clear all the entries, use close() for that.
        """
        if not self.is_running():
            return

        if not self._runner.done():
            self._runner.cancel()

    def close(self):
        """Closes the running task, and does any cleanup, if necessary."""
        self.stop()
        self.loop.create_task(self._cleanup())
        del self._callbacks[:]
        self._current = None


class QueueScheduler(BaseScheduler):
    """Original implementation of a scheduler.

    This uses an asyncio.PriorityQueue, which means all of the events are stored
    in memory. This can be very risky if too many entries are stored.
    """

    def __init__(self, **kwargs):
        super().__init(**kwargs)
        self._pending = asyncio.PriorityQueue()

    # We have to override _restart as well because _get removes the entry.
    def _restart(self):
        if self._current is not None:
            self._pending.put_nowait(self._current)
        super()._restart()

    async def _get(self):
        return await self._pending.get()

    async def _put(self, entry):
        self._pending.put_nowait(entry)

    async def _remove(self, entry):
        # Don't remove the entry if it's the current one because _get will
        # remove the entry from the queue.
        if entry == self._current:
            self._current = None  # Needed to tell _restart to not put the entry back in.
        else:
            self.pending._queue.remove(entry)
            heapq.heapify(self.pending._queue)


# Below here is the database form of the scheduler. If you want to just use the
# scheduler without worrying about using a DB, then ignore everything below here.
__schema__ = """
    CREATE TABLE IF NOT EXISTS schedule (
        id SERIAL PRIMARY KEY,
        expires TIMESTAMP NOT NULL,

        -- metadata
        event TEXT NOT NULL,
        created TIMESTAMP NOT NULL DEFAULT (now() at time zone 'utc'),
        args_kwargs JSONB NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX IF NOT EXISTS schedule_expires_idx ON schedule (expires);
"""

class DatabaseScheduler(BaseScheduler):
    """An implementation of a Scheduler where a database is used.

    Only DBMSs that support JSON types are supported (so basically just PostgresSQL).
    """
    __schema__ = __schema__

    def __init__(self, pool, *, safe_mode=True, **kwargs):
        super().__init__(**kwargs)
        self._pool = pool
        self._safe = safe_mode
        self._have_data = asyncio.Event()

    async def _dispatch(self, timer):
        await super()._dispatch(timer)

        # The entry was short, so there's no entry to remove in the database.
        if not getattr(timer, 'short', True):
            await self._remove(timer)

    # Overriding this because the two are datetime instances.
    @staticmethod
    def _calculate_delta(time1, time2):
        return (time1 - time2).total_seconds()

    async def _get_entry(self):
        # Don't make a new connection to avoid hanging the bot
        query = 'SELECT * FROM schedule ORDER BY expires LIMIT 1;'
        return await self._pool.fetchrow(query)

    async def _get(self):
        while True:
            entry = await self._get_entry()
            if entry is not None:
                self._have_data.set()
                return _Entry.from_record(entry)

            self._have_data.clear()
            self._current = None
            await self._have_data.wait()

    async def _put(self, entry):
        # put the entry in the database
        # We have to use a manual query because of the JSON type.
        query = """INSERT INTO schedule (created, event, expires, args_kwargs)
                   VALUES ($1, $2, $3, $4::jsonb)
                """

        await self._pool.execute(
            query,
            entry.created,
            entry.event,
            entry.time,
            {'args': entry.args, 'kwargs': entry.kwargs},
        )
        self._have_data.set()

    async def _remove(self, entry):
        # remove entry from the database
        try:
            query = 'DELETE FROM schedule WHERE id = $1'
            await self._pool.execute(query, entry.id)
        except Exception as e:
            # Something went terribly wrong with removing, so we gotta stop
            # the scheduler, otherwise we'd run into an infinite loop.
            if self._safe:
                self.stop()
            log.error('Removing %r failed. Exception: %r', entry, e)
            raise
