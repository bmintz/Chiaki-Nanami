import datetime
import inspect
import itertools
import json
import random
import re
import sys
import traceback

import discord
from discord.ext import commands
from more_itertools import last

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

_reverse_quotes = {cq: oq for oq, cq in commands.view._quotes.items()}
_clean_content = commands.clean_content(fix_channel_mentions=True, escape_markdown=True)

def _get_bad_argument(ctx, param):
    content = ctx.message.content
    view = ctx.view
    if param.kind == param.KEYWORD_ONLY and not ctx.command.rest_is_raw:
        # Keyword only arguments are interpreted as "consume rest", unless
        # rest_is_raw is True. Internally, this means that view.read_rest() is
        # called as opposed to commands.view.quoted_word(view). This means we
        # can just grab the whole string via view.previous.
        return content[view.previous:], view.previous

    # It's a non-consume-rest arg which means we need to figure out where
    # the bad argument started. Believe it or not this is extremely hard as
    # commands.view.quoted_word(view) calls view.get() repeatedly, which
    # corrupts view.previous as now it's only the previous character rather
    # than the whole word. What makes it worse are the quotes, as that means
    # we can't just find the "last word" in that string.
    #
    # An alternative way would be to monkey patch quoted_word to set something
    # like view.quoted_word_previous = view.index before calling the real
    # one, but such thing would be ridiculously horrid, and would probably
    # be more fragile, as it assumes that StringView isn't slotted and
    # quoted_word would stay "public".

    # Anything past view.index can't (or shouldn't) be checked for validity,
    # so we can safely discard it.
    content = ctx.message.content[:ctx.view.index]
    bad_quote = content[-1]
    bad_open_quote = _reverse_quotes.get(bad_quote)
    if not bad_open_quote or content[-2:-1] in ['\\', '']:
        # If there was no quote, or if it was escaped, then we can just
        # chomp off the whitespace up to that point.
        #
        # Use rsplit instead of rpartition as the former can take any
        # whitespace. (ext can take any sort of whitespace)
        bad_content = content.rsplit(None, 1)[-1]

        return bad_content, view.index - len(bad_content)

    # We need to look for the last "quoted" word.
    quote_pattern = rf'{bad_open_quote}(?:[^{bad_quote}\\]|\\.)*{bad_quote}'
    last_match = last(re.finditer(quote_pattern, content))
    # I swear if last_match is None...
    assert last_match, f'last_match is None with content {content}'
    return last_match[1], last_match.start()

async def _format_bad_argument(ctx, param, error):
    _, end_content_at = _get_bad_argument(ctx, param)
    content = ctx.message.content[:end_content_at]

    # The only reason why this function is async def
    content = await _clean_content.convert(ctx, content)

    # Show the rest of the args, because we're not using the original text past
    # the bad argument we may as well show valid examples.
    required, optional = map(iter, _split_params(ctx.command))
    # These are iterators so consuming them in chain will also consume them outside.
    next(itertools.dropwhile(lambda p: p != param, itertools.chain(required, optional)), None)
    example = f'**{next(_parameter_examples([param], ctx))}**'

    other_examples = ' '.join(_parameter_examples(itertools.chain(required, _random_slice(list(optional))), ctx))
    if other_examples:
        example = example + ' ' + other_examples

    return (
        f'{error}\n\n'
        f'Usage: `{ctx.clean_prefix}{ctx.command.signature}`\n'
        f'Example: {content}{example}'
    )

@_handler(commands.BadArgument)
async def _bad_argument(ctx, error):
    # This can probably be simplified if I made BadArgument raise
    # with something else.
    params = list(ctx.command.params.values())
    index = min(len(ctx.args) + len(ctx.kwargs), len(params) - 1)
    param = params[index]

    error = error or error.__cause__
    if isinstance(error.__cause__, ValueError):
        # Capture any errors caused by int or float. This is not limited to just
        # int and float, but also any converters that call them as well.
        cause = str(error.__cause__)
        if cause.startswith((
            'invalid literal for int()',         # int
            'could not convert string to float'  # float
        )):
            # This is the one of the few times we need to extract out the argument
            # because the error is bad. Thankfully it's easy to extract here.
            match = re.search(r": '(.*)'", cause)
            error = f'"{match[1]}" is not a number.'

    message = await _format_bad_argument(ctx, param, error)
    await ctx.send(message)

# ----------- BadUnionArgument -----------

def _format_converter(converter):
    if converter == int:
        return 'number'
    return converter.__name__.lower()

def _format_converters(converters):
    # Right now the only things I have are "int", "Member", and the channels.
    # When I make typing.Union a normal thing (which might never happen because
    # of disambiguation), this probably won't be as hacky.

    if all(inspect.isclass(c) and issubclass(c, discord.abc.GuildChannel) for c in converters):
        return 'channel'

    return human_join(map(_format_converter, converters), final='or')

@_handler(commands.BadUnionArgument)
async def _bad_union_argument(ctx, error):
    bad_argument, _ = _get_bad_argument(ctx, error.param)
    error_message = f'"{bad_argument}" is not a {_format_converters(error.converters)}.'

    message = await _format_bad_argument(ctx, error.param, error_message)
    await ctx.send(message)

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
