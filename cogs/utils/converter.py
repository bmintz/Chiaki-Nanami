import difflib
import random

from collections import OrderedDict
from discord.ext import commands

from .commands import command_category
from .examples import get_example, wrap_example

def _unique(iterable):
    return iter(OrderedDict.fromkeys(iterable))


class Category(commands.Converter):
    @staticmethod
    def __get_categories(ctx):
        return (command_category(c, 'other') for c in ctx.bot.commands)

    async def convert(self, ctx, arg):
        parents = set(map(str.lower, self.__get_categories(ctx)))
        lowered = arg.lower()
        if lowered not in parents:
            raise commands.BadArgument(f'"{arg}" is not a category.')
        return lowered

    @staticmethod
    def random_example(ctx):
        categories = set(map(str.title, Category.__get_categories(ctx)))
        return random.sample(categories, 1)[0]


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
