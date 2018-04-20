import asyncio
import functools
import inspect

from lru import LRU


_keyword_marker = object()


# Key-making functions
def unordered(args, kwargs):
    if kwargs:
        args += (_keyword_marker, *kwargs.items())
    return frozenset(args)


default_key = functools.partial(functools._make_key, typed=False)
typed_key = functools.partial(functools._make_key, typed=True)


# From Danny's cache.py, just with some modifications to allow for
# custom key args, and the strategy is determined by the maxsize arg.
# https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/utils/cache.py
def cache(maxsize=128, make_key=default_key):
    def decorator(func):
        if maxsize is None:
            cache = {}
            get_stats = lambda: (0, 0)
        else:
            cache = LRU(maxsize)
            get_stats = cache.get_stats

        def wrap_and_store(key, coro):
            async def func():
                value = await coro
                cache[key] = value
                return value
            return func()

        def wrap_new(value):
            async def new_coroutine():
                return value
            return new_coroutine()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = make_key(args, kwargs)
            # try/except might be slow if the key is constantly not in the cache.
            # I wonder if it's faster to use cache.get and compare to a sentinel.
            try:
                value = cache[key]
            except KeyError:
                value = func(*args, **kwargs)

                if inspect.isawaitable(value):
                    return wrap_and_store(key, value)

                cache[key] = value
                return value
            else:
                if asyncio.iscoroutinefunction(func):
                    return wrap_new(value)
                return value

        def invalidate(*args, **kwargs):
            # LRU.pop isn't a thing :(
            # Implementation if LRU.pop existed would be much simpler:
            #
            # _sentinel = object()
            # return cache.pop(make_key(args, kwargs), _sentinel) is not _sentinel
            try:
                del cache[make_key(args, kwargs)]
            except KeyError:
                return False
            else:
                return True

        wrapper.cache = cache
        wrapper.get_key = lambda *a, **kw: make_key(a, kw)
        wrapper.invalidate = invalidate
        wrapper.get_stats = get_stats
        return wrapper
    return decorator


async_cache = cache
