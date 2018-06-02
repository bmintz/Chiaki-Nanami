import argparse
import asyncio
import contextlib
import datetime
import discord
import functools
import logging
import os
import sys

from core import Chiaki

# use faster event loop, but fall back to default if on Windows or not installed
try:
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


@contextlib.contextmanager
def log(stream=False):
    logging.getLogger('discord').setLevel(logging.INFO)

    os.makedirs(os.path.join(os.path.dirname(__file__), 'logs'), exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.FileHandler(
        filename=f'logs/chiaki-{datetime.datetime.now()}.log',
        encoding='utf-8',
        mode='w'
    )
    fmt = logging.Formatter('[{asctime}] ({levelname:<7}) {name}: {message}', '%Y-%m-%d %H:%M:%S', style='{')
    handler.setFormatter(fmt)
    root.addHandler(handler)

    if stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)

    try:
        yield
    finally:
        for hdlr in root.handlers[:]:
            hdlr.close()
            root.removeHandler(hdlr)


#--------------MAIN---------------

_old_send = discord.abc.Messageable.send

async def new_send(self, content=None, *, allow_everyone=False, **kwargs):
    if content is not None:
        if not allow_everyone:
            content = str(content).replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')

    return await _old_send(self, content, **kwargs)


def main():
    # This has to be patched first because Chiaki loads her extensions in
    # __init__, which means she loads her commands in __init__
    from discord.ext import commands
    old_commands_group = commands.group
    commands.group = functools.partial(old_commands_group, case_insensitive=True)

    bot = Chiaki()

    parser = argparse.ArgumentParser()
    parser.add_argument('--create-tables', action='store_true', help='Create the tables before running the bot.')
    parser.add_argument('--log-stream', action='store_true', help='Adds a stderr stream-handler for logging')

    args = parser.parse_args()
    if args.create_tables:
        bot.loop.run_until_complete(bot.run_sql())

    discord.abc.Messageable.send = new_send
    with log(args.log_stream):
        try:
            bot.run()
        finally:
            discord.abc.Messageable.send = _old_send
            commands.group = old_commands_group
    return 69 * bot.reset_requested


if __name__ == '__main__':
    sys.exit(main())
