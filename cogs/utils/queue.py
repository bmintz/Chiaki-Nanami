import asyncio
import collections

class SimpleQueue:
    """A simple, unbounded FIFO queue."""
    # Based off of queue.SimpleQueue (3.7+)

    def __init__(self):
        self._queue = collections.deque()
        self._count = asyncio.Semaphore(0)

    def empty(self):
        """Return True if the queue is empty, False otherwise."""
        return not self._queue

    def qsize(self):
        """Number of items in the queue."""
        return len(self._queue)

    async def put(self, item):
        """Put an item into the queue.

        This is only a coroutine for compatibility with asyncio.Queue.
        """
        self.put_nowait(item)

    def put_nowait(self, item):
        """Put an item into the queue without blocking."""
        self._queue.append(item)
        self._count.release()

    async def get(self):
        """Remove and return an item from the queue.

        If queue is empty, wait until an item is available.
        """
        await self._count.acquire()
        return self.get_nowait()

    def get_nowait(self):
        """Remove and return an item from the queue.

        Return an item if one is immediately available, else raise QueueEmpty.
        """
        if self.empty():
            raise asyncio.QueueEmpty
        return self._queue.popleft()
