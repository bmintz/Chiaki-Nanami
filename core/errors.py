import datetime
import itertools
import json
import random
import sys
import traceback

import discord
from discord.ext import commands

from cogs.utils.examples import _parameter_examples, _split_params
from cogs.utils.formats import human_join
from cogs.utils.misc import emoji_url

_handlers = []

def _handler(*exceptions):
    def decorator(func):
        _handlers.append((exceptions, func))
        return func
    return decorator

# --------- BotMissingPermissions --------

_DEFAULT_MISSING_PERMS_ACTIONS = {
    'embed_links': 'embeds',
    'attach_files': 'upload stuffs',
}

with open('data/bot_missing_perms.json', encoding='utf-8') as f:
    _missing_perm_actions = json.load(f)

def _format_bot_missing_perms(ctx, missing_perms):
    action = _missing_perm_actions.get(str(ctx.command))
    if not action:
        actions = (
            _DEFAULT_MISSING_PERMS_ACTIONS.get(p, p.replace('_', ' '))
            for p in missing_perms
        )
        action = human_join(actions, final='or')

    nice_perms = (
        perm.replace('_', ' ').replace('guild', 'server').title()
        for perm in missing_perms
    )

    return (
        f"Hey hey, I don't have permissions to {action}. "
        f'Please check if I have {human_join(nice_perms)}.'
    )

@_handler(commands.BotMissingPermissions)
def bot_missing_perms(ctx, error):
    return ctx.send(_format_bot_missing_perms(ctx, error.missing_perms))

# -------- MissingRequiredArgument -------

def _random_slice(seq):
    return seq[:random.randint(0, len(seq))]

def _format_missing_required_arg(ctx, param):
    required, optional = _split_params(ctx.command)
    missing = list(itertools.dropwhile(lambda p: p != param, required))
    names = human_join(f'`{p.name}`' for p in missing)
    example = ' '.join(_parameter_examples(missing + _random_slice(optional), ctx))

    # TODO: Specify the args more descriptively.
    return (
        f"Hey hey, you're missing {names}.\n\n"
        f'Usage: `{ctx.clean_prefix}{ctx.command.signature}`\n'
        f'Example: {ctx.message.clean_content} **{example}** \n'
    )

@_handler(commands.MissingRequiredArgument)
def missing_required_arg(ctx, error):
    return ctx.send(_format_missing_required_arg(ctx, error.param))

# ----------- BadArgument -----------

@_handler(commands.BadArgument)
def _bad_argument(ctx, error):
    # TODO: Handle this properly when discord.py isn't stupid with BadArgument
    return ctx.send(error or error.__cause__)

# ----------- NoPrivateMessage ------------

@_handler(commands.NoPrivateMessage)
def bad_argument(ctx, _):
    return ctx.send('This command cannot be used in private messages.')

# ---------- CommandInvokeError ----------

@_handler(commands.CommandInvokeError)
async def command_invoke_error(ctx, error):
    print(f'In {ctx.command.qualified_name}:', file=sys.stderr)
    traceback.print_tb(error.original.__traceback__)
    print(f'{error.__class__.__name__}: {error}'.format(error), file=sys.stderr)

# ---------- Error Webhook ---------

_ignored_exceptions = (
    commands.NoPrivateMessage,
    commands.DisabledCommand,
    commands.CheckFailure,
    commands.CommandNotFound,
    commands.UserInputError,
    discord.Forbidden,
)

ERROR_ICON_URL = emoji_url('\N{NO ENTRY SIGN}')

async def _send_error_webhook(ctx, error):
    # command_counter['failed'] += 0 sets the 'failed' key. We don't want that.
    if not isinstance(error, commands.CommandNotFound):
        ctx.bot.command_counter['failed'] += 1

    webhook = ctx.bot.webhook
    if not webhook:
        return

    error = getattr(error, 'original', error)

    if isinstance(error, _ignored_exceptions) or getattr(error, '__ignore__', False):
        return

    e = (discord.Embed(colour=0xcc3366)
         .set_author(name=f'Error in command {ctx.command}', icon_url=ERROR_ICON_URL)
         .add_field(name='Author', value=f'{ctx.author}\n(ID: {ctx.author.id})', inline=False)
         .add_field(name='Channel', value=f'{ctx.channel}\n(ID: {ctx.channel.id})')
         )

    if ctx.guild:
        e.add_field(name='Guild', value=f'{ctx.guild}\n(ID: {ctx.guild.id})')

    exc = ''.join(traceback.format_exception(type(error), error, error.__traceback__, chain=False))
    e.description = f'```py\n{exc}\n```'
    e.timestamp = datetime.datetime.utcnow()
    await webhook.send(embed=e)

# ------------- Error handler -------------

async def on_command_error(ctx, error):
    await _send_error_webhook(ctx, error)

    if not ctx.__bypass_local_error__ and hasattr(ctx.command, 'on_error'):
        return

    for exc_type, handler in _handlers:
        if isinstance(error, exc_type):
            await handler(ctx, error)
            break


def setup(bot):
    bot.add_listener(on_command_error)

def teardown(bot):
    bot.remove_listener(on_command_error)