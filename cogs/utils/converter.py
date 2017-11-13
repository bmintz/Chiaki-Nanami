import argparse
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


# Custom ArgumentParser because the one in argparse raises SystemExit upon
# failure, which kills the bot
class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise commands.BadArgument(f'Failed to parse args.```\n{message}```')


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
        lowered = arg.lower()

        result = ctx.bot.get_cog(lowered)
        if result is None:
            raise commands.BadArgument(f"Module {lowered} not found")

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


def in_(*choices):
    def in_converter(arg):
        lowered = arg.lower()
        if lowered in choices:
            return lowered
        raise commands.BadArgument(f"{lowered} is not valid option. "
                                   f"Available options:\n{', '.join(choices)}")
    return in_converter


def ranged(low, high=None, *, type=int):
    'Converter to check if an argument is in a certain range INCLUSIVELY'
    if high is None:
        low, high = 0, low

    def ranged_argument(arg):
        result = type(arg)
        if low <= result <= high:
            return result
        raise commands.BadArgument(f'Value must be between {low} and {high}, '
                                   f'or equal to {low} or {high}.')
    return ranged_argument
