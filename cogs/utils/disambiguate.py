"""Converters for disambiguation.

By default, if there is more than one thing with a given name. The converters
will only pick the first result. These converters are made as a result to
be able to select from multiple things with the same name.

This becomes especially important when the args are case-insensitive.
"""

import discord
import functools
import random
import re

from discord.ext import commands
from more_itertools import always_iterable

from .context_managers import temp_attr
from .examples import get_example

# Avoid excessive dot-lookup every time a member is attempted to be converted
_get_from_guilds = commands.converter._get_from_guilds


class _DisambiguateExampleGenerator:
    # This must be a descriptor because it can either be called from
    # a class or an instance.
    def __get__(self, obj, cls):
        cls_name = cls.__name__.replace('Disambiguate', '')
        return functools.partial(get_example, getattr(discord, cls_name))


class DisambiguateConverter(commands.Converter):
    def __init__(self, *, case_sensitive=False):
        super().__init__()
        self.case_sensitive = case_sensitive

    random_example = _DisambiguateExampleGenerator()


class DisambiguateRole(DisambiguateConverter, commands.IDConverter):
    """Converter that allows for case-insensitive discord.Role conversion."""
    async def convert(self, ctx, argument):
        guild = ctx.guild
        if not guild:
            raise commands.NoPrivateMessage()

        # Let ID's and mentions take priority
        match = self._get_id_match(argument) or re.match(r'<@&([0-9]+)>$', argument)
        if match:
            predicate = lambda r, id=int(match.group(1)): r.id == id
        elif self.case_sensitive:
            predicate = lambda r: r.name == argument
        else:
            predicate = lambda r, arg=argument.lower(): r.name.lower() == arg

        return await ctx.disambiguate(list(filter(predicate, guild.roles)))


class DisambiguateMember(DisambiguateConverter, commands.MemberConverter):
    """Converter that allows for discord.Member disambiguation."""
    async def convert(self, ctx, argument):
        guild = ctx.guild
        bot = ctx.bot
        match = self._get_id_match(argument) or re.match(r'<@!?([0-9]+)>$', argument)

        # IDs must be unique, we won't allow conflicts here.
        if match is not None:
            return await super().convert(ctx, argument)

        # not a mention or ID...
        if guild:
            # The trailing comma is because the result expects a sequence
            # This will transform result into a tuple, which is important.
            # Because len(discord.Member) will error.
            result = guild.get_member_named(argument),
            # Will only be False if only the name was provided, or a user with
            # the same nickname as another user's fullname was provided.
            # (eg if someone nicknames themself "rjt#2336", and rjt#2336 was in
            # the server)
            if str(result[0]) != argument:
                if self.case_sensitive:
                    def predicate(m):
                        return m.name == argument or (m.nick and m.nick == argument)
                else:
                    lowered = argument.lower()

                    def predicate(m):
                        # We can't just do lowered in (m.nick.lower(), m.name.lower())
                        # because m.nick can be None
                        return m.name.lower() == lowered or (m.nick and m.nick.lower() == lowered)

                # filter it out from here
                result = list(filter(predicate, guild.members))

        else:
            # We can't use the "fuzzy" match here, due to potential conflicts
            # and duplicate results.
            #
            # The trailing comma is because the result expects a sequence
            # This will transform result into a tuple, which is important.
            # Because len(discord.Member) will error.
            result = _get_from_guilds(bot, 'get_member_named', argument),

        return await ctx.disambiguate(result)


class DisambiguateTextChannel(DisambiguateConverter, commands.TextChannelConverter):
    async def convert(self, ctx, argument):
        bot = ctx.bot

        match = self._get_id_match(argument) or re.match(r'<#([0-9]+)>$', argument)
        result = None
        guild = ctx.guild

        if match is not None:
            return await super().convert(ctx, argument)
        else:
            if self.case_sensitive:
                def check(c):
                    return isinstance(c, discord.TextChannel) and c.name == argument
            else:
                # not a mention
                lowered = argument.lower()

                def check(c):
                    return isinstance(c, discord.TextChannel) and c.name.lower() == lowered

            if guild:
                result = list(filter(check, guild.text_channels))
                transform = str
            else:
                result = list(filter(check, bot.get_all_channels()))
                transform = '{0} (Server: {0.guild})'

        return await ctx.disambiguate(result, transform)


class DisambiguateUser(DisambiguateConverter, commands.UserConverter):
    async def convert(self, ctx, argument):
        match = self._get_id_match(argument) or re.match(r'<@!?([0-9]+)>$', argument)
        state = ctx._state

        if match is not None:
            return await super().convert(ctx, argument)

        # check for discriminator if it exists
        # This should also be an exact match.
        if len(argument) > 5 and argument[-5] == '#':
            return await super().convert(ctx, argument)

        if self.case_sensitive:
            results = [u for u in state._users.values() if u.name == argument]
        else:
            lowered = argument.lower()
            results = [u for u in state._users.values() if u.name.lower() == lowered]

        return await ctx.disambiguate(results)


class DisambiguateGuild(DisambiguateConverter, commands.IDConverter):
    async def convert(self, ctx, arg):
        match = self._get_id_match(arg)
        state = ctx._state
        if match:
            guild = ctx.bot.get_guild(int(match.group(1)))
            if guild:
                return guild

        if self.case_sensitive:
            guilds = [g for g in state._guilds.values() if g.name == arg]
        else:
            lowered = arg.lower()
            guilds = [g for g in state._guilds.values() if g.name.lower() == lowered]

        return await ctx.disambiguate(guilds)


# A mapping for the discord.py class and it's corresponding searcher.
_type_search_maps = {
    discord.Role: DisambiguateRole,
    discord.Member: DisambiguateMember,
    discord.TextChannel: DisambiguateTextChannel,
    discord.User: DisambiguateUser,
    discord.Guild: DisambiguateGuild,
}


# Function for stubbing out the context's disambiguate method
# so it doesn't prematurely do the disambiguation prompt.
async def _dummy_disambiguate(matches, *args, **kwargs):
    return matches


class union(DisambiguateConverter, commands.Converter):
    def __init__(self, *types, case_sensitive=False):
        # Can't use super() because of weird MRO things
        self.types = types
        self.case_sensitive = case_sensitive

    async def convert(self, ctx, argument):
        choices = []

        with temp_attr(ctx, 'disambiguate', _dummy_disambiguate):
            for converter in self.searchers():
                try:
                    entries = await ctx.command.do_conversion(ctx, converter, argument)
                except commands.BadArgument:
                    continue
                else:
                    choices.extend(always_iterable(entries))

        return await ctx.disambiguate(choices, '{0} ({0.__class__.__name__})'.format)

    def searchers(self):
        return [
            _type_search_maps[t](case_sensitive=self.case_sensitive)
            if t in _type_search_maps else t
            for t in self.types
        ]

    def random_example(self, ctx):
        return get_example(random.choice(self.types), ctx)
