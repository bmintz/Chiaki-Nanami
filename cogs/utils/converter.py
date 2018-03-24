import difflib
import discord

from collections import OrderedDict
from discord.ext import commands


def _unique(iterable):
    return iter(OrderedDict.fromkeys(iterable))


class NoBots(commands.BadArgument):
    """Exception raised in CheckedMember when the author passes a bot"""


class NoOfflineMembers(commands.BadArgument):
    """Exception raised in CheckedMember when the author passes a user who is offline"""


class NoSelfArgument(commands.BadArgument):
    """Exception raised in CheckedMember when the author passes themself as an argument"""


class CheckedMember(commands.MemberConverter):
    def __init__(self, *, offline=True, bot=True, include_self=False):
        super().__init__()
        self.self = include_self
        self.offline = offline
        self.bot = bot

    async def convert(self, ctx, arg):
        member = await super().convert(ctx, arg)
        if member.status is discord.Status.offline and not self.offline:
            raise NoOfflineMembers(f'{member} is offline...')
        if member.bot and not self.bot:
            raise NoBots(f"{member} is a bot. You can't use a bot here.")
        if member == ctx.author:
            raise NoSelfArgument("You can't use yourself. lol.")

        return member


class BotCogConverter(commands.Converter):
    async def convert(self, ctx, arg):
        result = ctx.bot.get_cog(arg)
        if result is None:
            raise commands.BadArgument(f"Module {arg} not found")

        return result


class BotCommand(commands.Converter):
    async def convert(self, ctx, arg):
        cmd = ctx.bot.get_command(arg)
        if cmd is None:
            names = map(str, _unique(ctx.bot.walk_commands()))
            closest = difflib.get_close_matches(arg, names, cutoff=0.5)
            # Can't use f-strings because \ is not allowed in the {} parts
            # also + is faster than .format
            joined = 'Did you mean...\n' + '\n'.join(closest) if closest else ''
            raise commands.BadArgument(f"I don't recognized the {arg} command. {joined}")

        return cmd


def number(s):
    for typ in (int, float):
        try:
            return typ(s)
        except ValueError:
            continue
    raise commands.BadArgument(f"{s} is not a number.")


class union(commands.Converter):
    def __init__(self, *types):
        self.types = types

    async def convert(self, ctx, arg):
        for type_ in self.types:
            try:
                # small hack here because commands.Command.do_conversion expects a Command instance
                # even though it's not used at all
                return await ctx.command.do_conversion(ctx, type_, arg)
            except Exception as e:
                continue
        type_names = ', '.join([t.__name__ for t in self.types])
        raise commands.BadArgument(f"I couldn't parse {arg} successfully, "
                                   f"given these types: {type_names}")
