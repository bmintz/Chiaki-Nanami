import contextlib
from collections.abc import Sequence
from core.errors import ChiakiException

_sentinel = object()


@contextlib.contextmanager
def temp_attr(obj, attr, value):
    """Temporarily sets an object's attribute to a value"""
    old_value = getattr(obj, attr, _sentinel)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        if old_value is _sentinel:
            delattr(obj, attr)
        else:
            setattr(obj, attr, old_value)


@contextlib.contextmanager
def redirect_exception(*exceptions, cls=ChiakiException):
    """Context manager to re-raise exceptions with a proxy exception class.

    The exceptions can either be an exception type or a (exc_type, string) pair.
    """
    exceptions = dict(exc if isinstance(exc, Sequence) else (exc, None)
                      for exc in exceptions)
    try:
        yield
    except tuple(exceptions) as e:
        raise cls(exceptions[type(e)] or str(e)) from e


# asynccontextmanager when
class temp_message:
    """Sends a temporary message, then deletes it"""
    def __init__(self, destination, content=None, *, file=None, embed=None):
        self.destination = destination
        self.content = content
        self.file = file
        self.embed = embed

    async def __aenter__(self):
        self.message = await self.destination.send(self.content, file=self.file, embed=self.embed)
        return self.message

    async def __aexit__(self, exc_type, exc, tb):
        await self.message.delete()
