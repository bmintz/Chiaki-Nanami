#!/usr/bin/env python3

import asyncio
import contextlib
import datetime
import click
import discord
import functools
import importlib
import itertools
import logging
import os
import sys
import traceback

from cogs.utils import db
from core import Chiaki, migration

import config

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

@click.group(invoke_without_command=True)
@click.option('--log-stream', is_flag=True, help='Adds a stderr stream-handler for logging')
@click.pass_context
def main(ctx, log_stream):
    if ctx.invoked_subcommand is not None:
        return

    # This has to be patched first because Chiaki loads her extensions in
    # __init__, which means she loads her commands in __init__
    from discord.ext import commands
    old_commands_group = commands.group
    commands.group = functools.partial(old_commands_group, case_insensitive=True)

    bot = Chiaki()

    discord.abc.Messageable.send = new_send
    with log(log_stream):
        try:
            bot.run()
        finally:
            discord.abc.Messageable.send = _old_send
            commands.group = old_commands_group
    return 69 * bot.reset_requested


# ------------- DB-related stuff ------------------

async def _create_pool():
    psql = f'postgresql://{config.psql_user}:{config.psql_pass}@{config.psql_host}/{config.psql_db}'
    return await db.create_pool(psql, command_timeout=60)

async def _migrate(version='', downgrade=False, verbose=False):
    # click doesn't like None as a default so we have to settle with an empty string
    if not version:
        version = None
    
    for e in itertools.chain.from_iterable(Chiaki.find_extensions(e) or [e] for e in config.extensions):
        try:
            importlib.import_module(e)
        except:
            click.echo(f'Could not load {e}.\n{traceback.format_exc()}', err=True)
            return

    pool = await _create_pool()
    async with pool.acquire() as conn:
        await migration.migrate(version, connection=conn, downgrade=downgrade, verbose=verbose)

def _sync_migrate(version, downgrade, verbose):
    run = asyncio.get_event_loop().run_until_complete
    run(_migrate(version, downgrade=downgrade, verbose=verbose))

@main.command()
@click.option('--version', default='', metavar='[version]', help='Version to migrate to, defaults to latest')
@click.option('-v', '--verbose', is_flag=True)
def upgrade(version, verbose):
    """Upgrade the database to a version"""
    _sync_migrate(version, downgrade=False, verbose=verbose)
    click.echo('Upgrade successful! <3')

@main.command()
@click.option('--version', default='', metavar='[version]', help='Version to migrate to, defaults to latest')
@click.option('-v', '--verbose', is_flag=True)
def downgrade(version, verbose):
    """Downgrade the database to a version"""
    _sync_migrate(version, downgrade=True, verbose=verbose)
    click.echo('Downgrade successful! <3')


if __name__ == '__main__':
    sys.exit(main())
