import asyncio
import collections
import contextlib
import functools
import itertools
import re

import discord
from discord.ext import commands
from more_itertools import chunked, consume, iter_except, unique_everseen

from .misc import maybe_awaitable
from .queue import SimpleQueue


_Trigger = collections.namedtuple('_Trigger', 'emoji pattern blocking fallback')


def trigger(emoji, pattern=None, *, block=False, fallback=None):
    """Add a function that will be called with a certain reaction.

    If pattern is a string, it will be used as a regex pattern for
    messages to trigger that function.

    If fallback is a string, it will be used as a regex pattern like
    pattern. However, it will only be used if the bot can't add
    reactions.

    If block is True, reactions will be ignored for the duration of the
    execution.
    """
    def decorator(func):
        func.__trigger__ = _Trigger(emoji=emoji, pattern=pattern, blocking=block, fallback=fallback)
        return func
    return decorator



paginated = functools.partial(
    commands.bot_has_permissions,
    embed_links=True,
)

# Extract the predicate from the check...
_validate_context = paginated()(lambda: 0).__commands_checks__[0]


class _TriggerCooldown(commands.CooldownMapping):
    def __init__(self):
        super().__init__(commands.Cooldown(rate=5, per=2, type=commands.BucketType.user))

    def _bucket_key(self, tup):
        return tup

    def is_rate_limited(self, message_id, user_id):
        bucket = self.get_bucket((message_id, user_id))
        return bucket.update_rate_limit() is not None

_trigger_cooldown = _TriggerCooldown()


class _Callback(collections.namedtuple('_Callback', 'func blocking')):
    """Wrapper class to store both the resolved descriptor and blocking
    attribute."""

    __slots__ = ()

    # InteractiveSession.reaction_help relies on the wrapped function's __doc__
    @property
    def __doc__(self):
        return self.func.__doc__


class InteractiveSession:
    """Base class for all interactive sessions.

    Subclasses must implement 'default' method. If necessary, they can
    override 'start'.

    Example:
    class Thing(InteractiveSession):
        @reaction('\N{HEAVY BLACK HEART}')
        def default(self):
            return discord.Embed(description='hi myst \N{HEAVY BLACK HEART}')

        @reaction('\N{THINKING FACE}')
        def think(self):
            return discord.Embed(description='\N{THINKING FACE}')

    A page should either return a discord.Embed, or None if to indicate the
    page was invalid somehow. e.g. The page number given was ofut of bounds,
    or there were side effects associated with it.
    """

    # TODO: Context-less __init__
    def __init__(self, ctx):
        self.context = ctx
        self._bot = ctx.bot
        self._channel = ctx.channel
        self._users = {ctx.author.id}
        self._message = None
        self._blocking = False

        # XXX: Remove self._current
        self._current = None

        # Queue is used to avoid having reactions being ignored if they're
        # spammed or if a callback unexpectedly takes a long time.
        self._queue = SimpleQueue()

    def __init_subclass__(cls, *, stop_emoji='\N{BLACK SQUARE FOR STOP}', stop_pattern=None, stop_fallback='exit', **kwargs):
        super().__init_subclass__(**kwargs)
        cls._reaction_map = callbacks = collections.OrderedDict()

        # These are lists as opposed to dicts because we need to iterate 
        # through it to find a match, rather than looking it up in a table.
        cls._message_callbacks = message_callbacks = []
        cls._message_fallbacks = message_fallbacks = []
        seen_patterns = set()

        def trigger_iterator():
            # Can't use inspect.getmembers for two reasons:
            # 1. It sorts the members lexographically, which is not what
            #    we want. We want them in definition order.
            # 2. It resolves descriptors too early. This causes things like
            #    x = page('emoji')(partialmethod(meth, ...)) to not be registered.
            #
            name_members = itertools.chain.from_iterable(b.__dict__.items() for b in cls.__mro__)
            for name, member in unique_everseen(name_members, key=lambda p: p[0]):
                trigger = getattr(member, '__trigger__', None)
                if trigger is None:
                    continue

                # Resolve any descriptors ahead of time so we can do _reaction_map[emoji](self)
                resolved = getattr(cls, name)
                callback = _Callback(resolved, trigger.blocking)
                yield trigger.emoji, trigger.pattern, trigger.fallback, callback

            if stop_emoji or stop_pattern or stop_fallback:
                yield stop_emoji, stop_pattern, stop_fallback, _Callback(cls.stop, False)

        for emoji, pattern, fallback, callback in trigger_iterator():
            if emoji not in callbacks:
                callbacks[emoji] = callback

            if pattern and pattern not in seen_patterns:
                seen_patterns.add(pattern)
                message_callbacks.append((pattern, callback))

            if fallback and fallback not in seen_patterns:
                seen_patterns.add(fallback)
                message_fallbacks.append((fallback, callback))

    def check(self, reaction, _):
        """Extra checks for reactions"""
        return reaction.emoji in self._reaction_map

    async def add_reactions(self):
        """Add the reactions to the message"""
        for emoji in self._reaction_map:
            await self._message.add_reaction(emoji)

    def default(self):
        """Return the first embed to start the controller"""
        raise NotImplementedError

    async def start(self):
        """First thing that gets called"""
        self._current = embed = await maybe_awaitable(self.default)
        self._message = await self._channel.send(embed=embed)

    async def stop(self):
        """Stop running the controller"""
        await self._queue.put(None)

    async def cleanup(self, *, delete_after):
        """Clean up anything else after stopping"""
        msg = self._message
        method = msg.delete if delete_after else msg.clear_reactions
        with contextlib.suppress(Exception):
            await method()

    # XXX: Remove release_connection
    async def run(self, *, timeout=120, delete_after=True, release_connection=True):
        """Run the interactive loop"""
        _validate_context(self.context)
        if release_connection:
            with contextlib.suppress(AttributeError):
                await self.context.release()

        await self.start()
        if self._message is None:
            # start() was overridden but no message was set
            raise RuntimeError('start() must set self._message')

        message = self._message
        triggers = self._message_callbacks.copy()
        task = None
        listeners = []

        def listen(func):
            listeners.append(func)
            return self._bot.listen()(func)

        # XXX: Can we accomplish without context???
        if self._channel.permissions_for(self.context.me).add_reactions:
            task = self._bot.loop.create_task(self.add_reactions())

            @listen
            async def on_reaction_add(reaction, user):
                if (
                    not self._blocking
                    and reaction.message.id == message.id
                    and user.id in self._users
                    and self.check(reaction, user)
                    and not _trigger_cooldown .is_rate_limited(message.id, user.id)
                ):
                    callback, self._blocking = self._reaction_map[reaction.emoji]
                    cleanup = functools.partial(message.remove_reaction, reaction.emoji, user)
                    await self._queue.put((callback, cleanup))
        else:
            triggers.extend(self._message_fallbacks)

        if triggers:
            @listen
            async def on_message(msg):
                if (
                    self._blocking
                    or msg.channel != self._channel
                    or msg.author.id not in self._users
                ):
                    return

                patterns, callbacks = zip(*triggers)
                selectors = map(re.fullmatch, patterns, itertools.repeat(msg.content))
                callback = next(itertools.compress(callbacks, selectors), None)
                if callback is None:
                    return

                if _trigger_cooldown.is_rate_limited(message.id, msg.author.id):
                    return

                callback, self._blocking = callback
                await self._queue.put((callback, msg.delete))

        try:
            while True:
                # TODO: Would async_timeout be better here?
                try:
                    job = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break

                if job is None:
                    break

                callback, after = job

                result = await maybe_awaitable(callback, self)

                with contextlib.suppress(discord.HTTPException):
                    await after()

                self._blocking = False

                if result is None:
                    continue
                self._current = result  # backwards compat...

                try:
                    await message.edit(embed=result)
                except discord.NotFound:  # Message was deleted
                    break

        finally:
            for listener in listeners:
                self._bot.remove_listener(listener)

            if not (task is None or task.done()):
                task.cancel()

            consume(iter_except(self._queue.get_nowait, asyncio.QueueEmpty))
            await self.cleanup(delete_after=delete_after)

    interact = run  # backwards compat

    @property
    def reaction_help(self):
        return '\n'.join(itertools.starmap('{0} => {1.__doc__}'.format, self._reaction_map.items()))


# ------------- Paginator --------------

class Paginator(InteractiveSession):
    """Class that takes an iterable of entries and paginates them.

    This is what often comes to mind when people talk about 'paginators'
    """
    def __init__(self, ctx, entries, *, per_page=15, title=discord.Embed.Empty, colour=None, **kwargs):
        super().__init__(ctx, **kwargs)
        self._pages = tuple(chunked(entries, per_page))
        self._index = 0

        if colour is None:
            colour = ctx.bot.colour

        # These should probably be removed at some point in the future.
        self.title = title
        self.colour = colour

    def single_page(self):
        """Return True if there is only one page, False otherwise"""
        return len(self._pages) == 1

    def small(self):
        """Return True if there are five pages or less, False otherwise"""
        return len(self._pages) <= 5

    async def start(self):
        await super().start()
        if self.single_page():
            # Don't even start paginating if there is only one page.
            await self.stop()

    async def cleanup(self, *, delete_after):
        if not self.single_page():
            await super().cleanup(delete_after=delete_after)

    async def add_reactions(self):
        if self.single_page():
            return

        fast_forwards = {'\U000023ed', '\U000023ee'}
        small = self.small()

        for emoji in self._reaction_map:
            if not (small and emoji in fast_forwards):
                await self._message.add_reaction(emoji)

    # Main methods
    def create_embed(self, page):
        """Create an embed given a slice of entries"""

        return (discord.Embed(title=self.title, colour=self.colour, description='\n'.join(page))
                .set_footer(text=f'Page: {self._index + 1} / {len(self._pages)} ({self.total} total)')
                )

    def page_at(self, idx):
        """Return the embed that would be created at a certain point.

        None if the index is out of bounds.
        """
        if not 0 <= idx < len(self._pages):
            return None

        self._index = idx
        return self.create_embed(self._pages[idx])

    @trigger('\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}', fallback=r'\<\<')
    def default(self):
        """First page"""
        return self.page_at(0)

    @trigger('\N{BLACK LEFT-POINTING TRIANGLE}',  fallback=r'\<')
    def previous(self):
        """Previous page"""
        return self.page_at(self._index - 1)

    @trigger('\N{BLACK RIGHT-POINTING TRIANGLE}',  fallback=r'\>')
    def next(self):
        """Next page"""
        return self.page_at(self._index + 1)

    @trigger('\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}', fallback=r'\>\>')
    def last(self):
        """Last page"""
        return self.page_at(len(self._pages) - 1)

    # --------- Go-to page ------------

    def _goto_embed(self):
        ctx = self.context
        description = (
            f'Please enter a number from 1 to {len(self._pages)}.\n\n'
            'To cancel, click \N{INPUT SYMBOL FOR NUMBERS} again.'
        )
        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name=f'What page do you want to go to, {ctx.author.display_name}?')
                )

    def _goto_parse_input(self, content):
        try:
            index = int(content)
        except ValueError:
            return None

        return self.page_at(index - 1)

    # XXX: This needs to be fully refactored for the reaction-less paginator
    #      or possibly not used at all.
    # XXX: This needs to use the user who actually added the reaction, NOT ctx.author
    @trigger('\N{INPUT SYMBOL FOR NUMBERS}', block=True)
    async def goto(self):
        """Go to page"""
        ctx = self.context
        return_result = None
        user_message = None

        def check(m):
            nonlocal return_result, user_message
            if not (m.channel.id == self._channel.id and m.author.id == ctx.author.id):
                return False

            result = self._goto_parse_input(m.content)
            if result is None:
                return False

            return_result = result
            user_message = m
            return True

        def remove_check(reaction, user):
            return (reaction.message.id == self._message.id
                    and user.id == ctx.author.id
                    and reaction.emoji == '\N{INPUT SYMBOL FOR NUMBERS}')

        # The two futures are such that so the user doesn't get "stuck" in the
        # number page. If they click on the number page by accident, then they
        # should have an easy way out.
        #
        # Thus we have to wait for one of two things:
        # 1. The actual number of the page they want to go to
        # 2. The removal of the numbered reaction if the user wants to go back

        to_wait = [
            self._bot.wait_for('message', check=check),
            self._bot.wait_for('reaction_remove', check=remove_check),
        ]

        try:
            embed = self._goto_embed()
            delete_always = await self._channel.send(embed=embed)

            done, pending = await asyncio.wait(
                to_wait,
                timeout=60,
                return_when=asyncio.FIRST_COMPLETED
            )
            for fut in pending:
                fut.cancel()

            if not done:
                # Effectively a timeout.
                return None

            result = done.pop().result()

            if isinstance(result, discord.Message):
                return return_result
            # The user probably removed a reaction.
            return None
        finally:
            for m in [delete_always, user_message]:
                with contextlib.suppress(Exception):
                    await m.delete()

    # ------------- End go-to page -----------

    @property
    def total(self):
        """Return the total number of entries in the list"""
        return sum(map(len, self._pages))

# -------------- Field Pages ----------------------

class EmbedFieldPages(Paginator):
    """Similar to Paginator, but uses the fields instead of the description"""
    def __init__(self, context, entries, *, inline=True, **kwargs):
        super().__init__(context, entries, **kwargs)

        self.inline = inline
        if len(self._pages) > 25:
            raise ValueError("too many fields per page (maximum 25)")

    def create_embed(self, page):
        embed = (discord.Embed(title=self.title, colour=self.colour)
                 .set_footer(text=f'Page: {self._index + 1} / {len(self._pages)} ({self.total} total)')
                 )

        add_field = functools.partial(embed.add_field, inline=self.inline)
        for name, value in page:
            add_field(name=name, value=value)
        return embed
