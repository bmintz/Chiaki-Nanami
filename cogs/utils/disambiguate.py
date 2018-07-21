import functools
import random
import re
import weakref

import discord
from discord.ext import commands

from .examples import get_example


class _DisambiguateExampleGenerator:
    # This must be a descriptor because it can either be called from
    # a class or an instance.
    def __get__(self, obj, cls):
        cls_name = cls.__name__.replace('Disambiguate', '')
        return functools.partial(get_example, getattr(discord, cls_name))

async def _disambiguate(ctx, matches, **kwargs):
    try:
        return await ctx.disambiguate(matches, **kwargs)
    except ValueError as e:
        # It doesn't matter if it's a Union or not, because there might be more
        # than one valid result anyways.
        raise commands.BadArgument(str(e)) from e

class Converter(commands.Converter):
    """Base class for all disambiguating converters.

    By default, if there is more than one thing with a given name, the
    ext converters will only pick the first result. These allow you to
    pick from multiple results.

    This becomes especially important when the args are case-insensitive.
    """
    _transform = str
    __converters__ = weakref.WeakValueDictionary()
    random_example = _DisambiguateExampleGenerator()

    def __init__(self, *, ignore_case=True):
        self.ignore_case = ignore_case

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        Converter.__converters__[cls.__name__] = cls

    def _get_possible_entries(self, ctx):
        """Return an iterable of possible entries to find matches with

        Subclasses must provide this to allow disambiguating.
        """
        raise NotImplementedError

    def _exact_match(self, ctx, argument):
        """Return an "exact match" given an argument.

        If this returns anything but None, that result will be
        returned without going through disambiguating.

        Subclasses may override this to provide an "exact" functionality.
        """
        return None

    # These predicates can be overridden if necessary

    def _predicate(self, obj, argument):
        """Standard predicate for filtering"""
        return obj.name == argument

    def _predicate_ignore_case(self, obj, argument):
        """Predicate for case-insensitive filtering"""
        return obj.name.lower() == argument

    # End of predicates

    def _get_possible_results(self, ctx, argument):
        entries = self._get_possible_entries(ctx)
        if self.ignore_case:
            lowered = argument.lower()
            predicate = self._predicate_ignore_case
        else:
            lowered = argument
            predicate = self._predicate

        return [obj for obj in entries if predicate(obj, lowered)]

    async def convert(self, ctx, argument):
        exact_match = self._exact_match(ctx, argument)
        if exact_match is not None:
            return exact_match

        matches = self._get_possible_results(ctx, argument)
        return await _disambiguate(ctx, matches, transform=self._transform)


_ID_REGEX = re.compile(r'([0-9]{15,21})$')

class IDConverter(Converter):
    def _get_from_id(self, ctx, id):
        """Given an ID, return an object via that ID"""
        raise NotImplementedError

    def __get_id_from_mention(self, argument):
        return re.match(self.MENTION_REGEX, argument) if self.MENTION_REGEX else None

    def _exact_match(self, ctx, argument):
        match = _ID_REGEX.match(argument) or self.__get_id_from_mention(argument)
        if not match:
            return None

        return self._get_from_id(ctx, int(match[1]))


class UserConverterMixin:
    MENTION_REGEX = r'<@!?([0-9]+)>$'

    def _exact_match(self, ctx, argument):
        result = super()._exact_match(ctx, argument)
        if result is not None:
            return result

        if not (len(argument) > 5 and argument[-5] == '#'):
            # We don't have a discriminator so we can't exact-match
            return None

        name, _, discriminator = argument.rpartition('#')
        return discord.utils.find(
            lambda u: u.name == name and u.discriminator == discriminator,
            self._get_possible_entries(ctx)
        )


class User(UserConverterMixin, IDConverter):
    def _get_from_id(self, ctx, id):
        return ctx.bot.get_user(id)

    def _get_possible_entries(self, ctx):
        return ctx._state._users.values()


class Member(UserConverterMixin, IDConverter):
    def _get_from_id(self, ctx, id):
        return ctx.guild.get_member(id)

    def _get_possible_entries(self, ctx):
        return ctx.guild._members.values()

    # Overriding these is necessary due to members having nicknames
    def _predicate(self, obj, argument):
        return super()._predicate(obj, argument) or (obj.nick and obj.nick == argument)

    def _predicate_ignore_case(self, obj, argument):
        return (
            super()._predicate_ignore_case(obj, argument)
            or (obj.nick and obj.nick.lower() == argument)
        )


class Role(IDConverter):
    MENTION_REGEX = r'<@&([0-9]+)>$'

    def _get_from_id(self, ctx, id):
        return discord.utils.get(self._get_possible_entries(), id=id)

    def _get_possible_entries(self, ctx):
        return ctx.guild.roles


class TextChannel(IDConverter):
    MENTION_REGEX = r'<#([0-9]+)>$'

    def _get_from_id(self, ctx, id):
        return ctx.guild.get_channel(id)

    def _get_possible_entries(self, ctx):
        return ctx.guild.text_channels


class Guild(IDConverter):
    MENTION_REGEX = None

    def _get_from_id(self, ctx, id):
        return ctx.bot.get_guild(id)

    def _get_possible_entries(self, ctx):
        return ctx._state._guilds.values()


def _is_discord_py_type(cls):
    module = getattr(cls, '__module__', '')
    return module.startswith('discord.') and not module.endswith('converter')


def _disambiguated(type_):
    """Return the corresponding disambiguating converter if one exists
    for that type.

    If no such converter exists, it returns the type.
    """
    if not _is_discord_py_type(type_):
        return type_

    return Converter.__converters__.get(type_.__name__, type_)

# https://github.com/Rapptz/discord.py/commit/2321ae8d9766779cef9baa7cc299e72a2ac88141
# adds a new "param" argument to Command.do_conversion which ends up breaking
# most Union converters. This is why typing.Union became supported in the next
# commit as it became impossible to properly do a Union without knowing what the
# current parameter is.
#
# Despite that we still need to maintain the disambiguate.Union so we need to do
# this.
def _get_current_parameter(ctx):
    parameters = list(ctx.command.params.values())

    # Need to account for varargs and consume-rest kwarg only
    index = min(len(ctx.args) + len(ctx.kwargs), len(parameters) - 1)
    return parameters[index]

    
class union(commands.Converter):
    _transform = '{0} ({0.__class__.__name__})'.format

    def __init__(self, *types, ignore_case=True):
        self.types = [
            type_(ignore_case=ignore_case)
            if isinstance(type_, type) and issubclass(type_, Converter)
            else type_
            for type_ in map(_disambiguated, types)
        ]

    async def convert(self, ctx, argument):
        param = _get_current_parameter(ctx)
        results = []
        for converter in self.types:
            # If we have a disambiguate converter then we must handle that
            # differently as the converter.convert prompts the user to choose.
            if isinstance(converter, Converter):
                exact = converter._exact_match(ctx, argument)
                if exact is not None:
                    return exact

                results.extend(converter._get_possible_results(ctx, argument))
            else:
                # It's just a standard type, just apply the standard conversion
                try:
                    result = await ctx.command.do_conversion(ctx, converter, argument, param)
                except commands.BadArgument:
                    continue
                else:
                    results.append(result)
        return await _disambiguate(ctx, results, transform=self._transform)

    def random_example(self, ctx):
        return get_example(random.choice(self.types), ctx)

Union = union
