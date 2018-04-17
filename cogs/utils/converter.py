import difflib
import random

from collections import OrderedDict
from discord.ext import commands

from .examples import get_example, wrap_example

def _unique(iterable):
    return iter(OrderedDict.fromkeys(iterable))


class BotCogConverter(commands.Converter):
    async def convert(self, ctx, arg):
        result = ctx.bot.get_cog(arg)
        if result is None:
            raise commands.BadArgument(f"Module {arg} not found")

        return result

    @staticmethod
    def random_example(ctx):
        return random.sample(ctx.bot.cogs.keys(), 1)[0]


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

    @staticmethod
    def random_example(ctx):
        return random.sample(set(ctx.bot.walk_commands()), 1)[0]


def number(s):
    for typ in (int, float):
        try:
            return typ(s)
        except ValueError:
            continue
    raise commands.BadArgument(f"{s} is not a number.")

@wrap_example(number)
def _number_example(ctx):
    return get_example(random.choice([int, float]), ctx)


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

    def random_example(self, ctx):
        return get_example(random.choice(self.types), ctx)
