"""Utilities to produce examples based off of args"""

import functools
import itertools
import operator
import random
import re
import typing

import discord
from discord.ext import commands
from more_itertools import iterate

from . import varpos
from .commands import all_qualified_names

__all__ = [
    'command_example',
    'get_example',
    'static_example',
    'wrap_example',
]

_random_generators = {}

# Stuff for builtins or discord.py models

def _example(func):
    _random_generators[typing.get_type_hints(func)['return']] = func
    return func

@_example
def _random_int(ctx) -> int:
    return random.randint(1, 100)

@_example
def _random_float(ctx) -> float:
    f = float(f'{random.randint(0, 100)}.{random.randint(0, 99)}')
    return int(f) if f.is_integer() else f

@_example
def _random_text_channel(ctx) -> discord.TextChannel:
    return f'#{random.choice(ctx.guild.text_channels)}'

@_example
def _random_voice_channel(ctx) -> discord.VoiceChannel:
    return random.choice(ctx.guild.voice_channels)

@_example
def _random_category_channel(ctx) -> discord.CategoryChannel:
    return random.choice(ctx.guild.categories)

@_example
def _random_user(ctx) -> discord.User:
    return random.choice(ctx.bot.users)

@_example
def _random_member(ctx) -> discord.Member:
    return f'@{random.choice(ctx.guild.members)}'

@_example
def _random_guild(ctx) -> discord.Guild:
    guild = random.choice(ctx.bot.guilds)
    return random.choice([guild.id, guild.name])

@_example
def _random_role(ctx) -> discord.Role:
    return random.choice(ctx.guild.roles)

@_example
def _random_bool(ctx) -> bool:
    return random.choice([True, False])


_DEFAULT_TEXT = 'Lorem ipsum dolor sit amet.'
_arg_examples = {}

def _load_arg_examples():
    import json, logging  # noqa
    try:
        with open('data/arg_examples.json', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        logging.getLogger(__name__).exception('failed to load static examples')
    else:
        _arg_examples.update(data)

_load_arg_examples()

def _get_static_example(key, command=''):
    for k in (f'{command}.{key}', key):
        examples = _arg_examples.get(k)
        if not examples:
            continue

        if isinstance(examples, list):
            return random.choice(examples)
        return examples

    return _DEFAULT_TEXT

def _actual_command(ctx):
    # help command is actually kinda a proxy command. When we do
    # "->help command" we most likely want the actual command
    # that was passed rather than the help command itself.
    command = ctx.command
    # help command's signature is "async def help(self, ctx, *, command)"
    return ctx.kwargs['command'] if command.name == 'help' else command

@_example
def _random_str(ctx) -> str:
    # XXX: Relies heavily on side-effects from context.
    return _get_static_example(ctx._current_parameter.name, _actual_command(ctx))


# Other random internal functions

def _get_name(obj):
    try:
        return obj.__name__
    except AttributeError:
        return obj.__class__.__name__

def _is_discord_ext_converter(converter):
    module = getattr(converter, '__module__', '')
    return module.startswith('discord') and module.endswith('converter')

def _always_classy(obj):
    return obj if isinstance(obj, type) else type(obj)

# Copied from inspect module
def _format_annotation(annotation, base_module=None):
    if getattr(annotation, '__module__', None) == 'typing':
        return repr(annotation).replace('typing.', '')
    if isinstance(annotation, type):
        if annotation.__module__ in ('builtins', base_module):
            return annotation.__qualname__
        return f'{annotation.__module__}.{annotation.__qualname__}'
    return repr(annotation)


# ------------- Main Example Function -----------

def get_example(converter, ctx):
    """Return a random example based on the converter"""
    if hasattr(converter, 'random_example'):
        return converter.random_example(ctx)

    # The default ext converters are special... maybe a bit too special.
    if _is_discord_ext_converter(converter):
        # commands.clean_content is really just str with some fancy formatting.
        if _always_classy(converter) is commands.clean_content:
            converter = str
        else:
            converter = getattr(discord, _get_name(converter).replace('Converter', ''))

    try:
        func = _random_generators[converter]
    except KeyError as e:
        raise ValueError(f'could not get an example for {_format_annotation(converter)}') from e
    else:
        return func(ctx)


# --------- Helper functions ---------

def wrap_example(target):
    """Wrap a converter to use a function for example generation"""
    def decorator(func):
        target.random_example = func
        return func
    return decorator

def static_example(converter):
    """Mark a converter to use the "static" example generator (str)"""
    converter.random_example = _random_str
    return converter

# -------------- Example generation --------------

# Needed to properly get the converter from a parameter
_get_converter = functools.partial(commands.Command._get_converter, None)


_quote_pattern = "|".join(map(re.escape, commands.view._all_quotes))

# This regex is here so that we only escape quotes that weren't escaped
# already, courtesy of https://gist.github.com/getify/3667624
_quote_regex = re.compile(rf'\\(.)|({_quote_pattern})', re.DOTALL)
_escape_quotes = functools.partial(_quote_regex.sub, r'\\\1\2')

def _quote(s):
    s = _escape_quotes(s)

    # In ext.commands quoting is NOT like shlex.quote. We only need
    # to surround it with quotes if there are spaces in the argument.
    #
    # Also prefer using double quotes. Even though all forms of Unicode
    # quotes are allowed now (as of ea061ef9b2ba64524b91af6036a615352ff9ce1a),
    # people will be more used to "".
    if any(map(str.isspace, s)):
        s = f'"{s}"'
    return s

def _is_required_parameter(param):
    return param.default is param.empty and param.kind is not param.VAR_POSITIONAL


MAX_REPEATS_FOR_VARARGS = 4


def _parameter_examples(parameters, ctx, command=None):
    command = command or ctx.command

    def parameter_example(parameter):
        # Need this for str and other static stuff
        ctx._current_parameter = parameter
        # example doesn't have to be str
        example = str(get_example(_get_converter(parameter), ctx))

        if not (
            parameter.kind == parameter.KEYWORD_ONLY
            and not command.rest_is_raw
            or example.startswith(('#', '@'))  # assume these are mentions
        ):
            # In ext.commands the keyword only argument is usually
            # meant to be passed as is, WITHOUT any quotes. This is
            # commonly known as the "consume rest" parameter and thus
            # SHOULD NOT be quoted.
            example = _quote(example)
        return example

    for param in parameters:
        yield parameter_example(param)
        if param.kind is param.VAR_POSITIONAL:
            for _ in range(random.randint(varpos.requires_var_positional(param), MAX_REPEATS_FOR_VARARGS)):
                yield parameter_example(param)


def _split_params(command):
    """Split a command's parameters into required and optional parts"""

    # Ignore self and ctx as they're automatically passed
    params = command.clean_params.values()
    required = list(itertools.takewhile(_is_required_parameter, params))

    optional = []
    # can't reuse the iterator used in takewhile as it throws out the
    # first element that returns False
    for param in itertools.dropwhile(_is_required_parameter, params):
        if param.kind is param.VAR_POSITIONAL:
            args = required if varpos.requires_var_positional(command) else optional
            args.append(param)
            break

        optional.append(param)
        if param.kind is param.KEYWORD_ONLY:
            break
    return required, optional

def command_example(command, ctx):
    """Generate a working example given a command.

    If a command has optional arguments, it will generate two examples,
    one with required arguments only, and one with all args included.
    """
    qual_names = list(all_qualified_names(command))

    required, optional = _split_params(command)

    def generate(params):
        resolved = ' '.join(_parameter_examples(params, ctx, command))
        return f'`{ctx.clean_prefix}{random.choice(qual_names)} {resolved}`'

    usage = generate(required)
    if not optional:
        return usage
    joined = '\n'.join(generate(required + optional[:i+1]) for i in range(len(optional)))
    return f'{usage}\n{joined}'
