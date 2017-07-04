import asyncio
import functools
import inspect
import json
import logging
import os
import random
import re

from collections import namedtuple, OrderedDict
from datetime import datetime, timezone
from discord.ext import commands
from more_itertools import grouper


REGIONAL_INDICATORS = [chr(i + 0x1f1e6) for i in range(26)]

def code_say(bot, msg):
    return bot.say(code_msg(msg))

def code_msg(msg, style=''):
    return f'```{style}\n{msg}```'

def cycle_shuffle(iterable):
    saved = [elem for elem in iterable]
    while True:
        random.shuffle(saved)
        for element in saved:
              yield element

def multi_replace(string, replacements):
    substrs = sorted(replacements, key=len, reverse=True)
    pattern = re.compile("|".join(map(re.escape, substrs)))
    return pattern.sub(lambda m: replacements[m.group(0)], string)

_markdown_replacements = {c: f'\\{c}' for c in ('*', '`', '_', '~', '\\')}
escape_markdown = functools.partial(multi_replace, replacements=_markdown_replacements)

def truncate(s, length, placeholder):
    return (s[:length] + placeholder) if len(s) > length + len(placeholder) else s

def str_join(delim, iterable):
    return delim.join(map(str, iterable))

def group_strings(string, n):
    return map(''.join, grouper(string, n, ''))

def pairwise(t):
    it = iter(t)
    return zip(it, it)

def nice_time(time):
    return time.strftime("%d/%m/%Y %H:%M")

def parse_int(maybe_int, base=10):
    try:
        return int(maybe_int, base)
    except ValueError:
        return None

def duration_units(secs):
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    w, d = divmod(d, 7)
    unit_list = [(w, 'weeks'), (d, 'days'), (h, 'hours'), (m, 'mins'), (s, 'seconds')]
    return ', '.join([f"{round(n)} {u}" for n, u in unit_list if n])

def ordinal(num):
    # pay no attention to this ugliness
    return "%d%s" % (num, "tsnrhtdd"[(num//10%10!=1)*(num%10<4)*num%10::4])

def file_handler(name, path='./logs', *, format='%(asctime)s/%(levelname)s: %(name)s: %(message)s'):
    now = datetime.now()
    os.makedirs(path, exist_ok=True)
    handler = logging.FileHandler(filename=f'{path}/{name}{now : %Y-%m-%d %H.%M.%S.%f.txt}.log', encoding='utf-8', mode='w')
    handler.setFormatter(logging.Formatter(format))
    return handler

def base_filename(name):
    return os.path.splitext(os.path.basename(name))[0]

def emoji_url(emoji):
    return f'https://twemoji.maxcdn.com/2/72x72/{hex(ord(emoji))[2:]}.png'

def unique(iterable):
    return list(OrderedDict.fromkeys(iterable))

async def maybe_awaitable(func, *args, **kwargs):
    maybe = func(*args, **kwargs)
    return await maybe if inspect.isawaitable(maybe) else maybe

def role_name(member, role):
    name = str(role)
    return f'**{escape_markdown(name)}**' if role in member.roles else name

async def load_async(filename, loop=None):
    loop = loop or asyncio.get_event_loop()

    def nobody_kanna_cross_it():
        with open(filename, encoding='utf-8') as f:
            return json.load(f)

    return await loop.run_in_executor(None, nobody_kanna_cross_it)