import asyncio
import inspect
import json
import os

from collections import OrderedDict
from more_itertools import grouper


REGIONAL_INDICATORS = [chr(i + 0x1f1e6) for i in range(26)]


def truncate(s, length, placeholder):
    return (s[:length] + placeholder) if len(s) > length + len(placeholder) else s


def str_join(delim, iterable):
    return delim.join(map(str, iterable))


def group_strings(string, n):
    return map(''.join, grouper(n, string, ''))


def nice_time(time):
    return time.strftime("%d/%m/%Y %H:%M")


def ordinal(num):
    # pay no attention to this ugliness
    return "%d%s" % (num, "tsnrhtdd"[(num//10%10!=1)*(num%10<4)*num%10::4])


def base_filename(name):
    return os.path.splitext(os.path.basename(name))[0]


def emoji_url(emoji):
    hexes = '-'.join(hex(ord(c))[2:] for c in str(emoji))
    return f'https://twemoji.maxcdn.com/2/72x72/{hexes}.png'


def unique(iterable):
    return list(OrderedDict.fromkeys(iterable))


async def maybe_awaitable(func, *args, **kwargs):
    maybe = func(*args, **kwargs)
    return await maybe if inspect.isawaitable(maybe) else maybe


async def load_async(filename, loop=None):
    loop = loop or asyncio.get_event_loop()

    def nobody_kanna_cross_it():
        with open(filename, encoding='utf-8') as f:
            return json.load(f)

    return await loop.run_in_executor(None, nobody_kanna_cross_it)
