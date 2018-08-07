import discord
import json
import pathlib
import random
import re

from collections import defaultdict, namedtuple
from discord.ext import commands
from itertools import zip_longest


Result = namedtuple('Result', 'cmp name image')

class RockPaperScissors:
    @staticmethod
    def pick(choice, elements):
        beats = choice.beats
        # Bias it towards winning elements
        weights = [(e not in beats) * 0.5 + 0.5 for e in elements]
        return Choice(*random.choices(list(elements.items()), weights)[0])

    @staticmethod
    def _result(choice1, choice2, ctx):
        if choice1.element is _null_element or choice1.name.lower() in choice2.beats:
            return Result(-1, "I", ctx.bot.user.avatar_url)

        if choice1.element == choice2.element:
            return Result(0, "It's a tie. No one", None)

        return Result(1, ctx.author.display_name, ctx.author.avatar_url)

    async def _rps(self, ctx, elem, game_type):
        choice = self.pick(elem, game_type.elements)
        cmp, name, thumbnail = self._result(elem, choice, ctx)

        # Get the outcome
        if cmp:
            winner, loser = (elem, choice)[::cmp]
            key = f'{winner.lower()}|{loser.lower()}'
            outcome = re.sub(
                r'\{(.*?)\}',
                lambda m: game_type.elements[m[1].lower()].emoji,  # r'**\1**',
                game_type.outcomes.get(key, f'{{{winner}}} did a thing.')
            )
        else:
            outcome = 'Well, this is awkward...'

        description = (
            f'{elem.emoji} {ctx.author.display_name} chose **{elem}**\n'
            f'{choice.emoji} I chose **{choice}**\n'
            f'\u200b\n{outcome}\n'
            f'**{name}** wins!!'
        )

        embed = (discord.Embed(colour=0x00FF00, description=description)
                 .set_author(name=game_type.title)
                 )

        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        await ctx.send(embed=embed)


def _distribute(n, seq):
    return [seq[i::n] for i in range(n)]

class GameType(namedtuple('RPSGameType', 'title elements outcomes')):
    __slots__ = ()

    def format_elements(self):
        # self.elements: name -> Element
        name_emoji = ((elem.emoji, name) for name, elem in self.elements.items())
        # Mobile is a bitch.
        per_row = -(-len(self.elements) // 3)
        rows = _distribute(per_row, sorted(name_emoji, key=lambda p: len(p[1])))

        widths = [
            # We want the names' length not the tuples.
            max(len(c[1]) for c in column)
            for column in zip_longest(*rows, fillvalue=('', ''))
        ]

        formats = [f'{{{i}[0]}}`{{{i}[1]:<{w + 1}}}\u200b`' for i, w in enumerate(widths)]

        return '\n'.join(
            ' '.join(formats[:len(row)]).format(*row)
            for row in rows
        )

    def element_embed(self):
        return (discord.Embed(colour=0xFFC107, description=self.format_elements())
                .set_author(name='Please type an element')
                )


Element = namedtuple('Element', 'emoji beats')
_null_element = Element('\u2754', set())

# TODO: Dataclasses?
class Choice(namedtuple('Choice', 'name element')):
    __slots__ = ()

    def __str__(self):
        return self.name

    def lower(self):
        return self.name.lower()

    @property
    def beats(self):
        return self.element.beats

    @property
    def emoji(self):
        return self.element.emoji


class RPSElement(commands.Converter):
    def __init__(self, type):
        self.game_type = type

    async def convert(self, ctx, arg):
        lowered = arg.lower()
        if lowered in ('chiaki', 'chiaki nanami'):
            raise commands.BadArgument("Hey, I'm not an RPS object!")
        if lowered == 'element':
            raise commands.BadArgument("Please don't be literal. Type an actual element.")

        elements = self.game_type.elements
        try:
            element = elements[lowered]
        except KeyError:
            arg, element = next(
                (p for p in elements.items() if p[1].emoji == lowered),
                (arg, _null_element)
            )

        return Choice(arg, element)

    def random_example(self, ctx):
        name, element = random.choice(list(self.game_type.elements.items()))
        return random.choice([name, element.emoji])


__warned_about_bad_element = set()


def _make_rps_command(name, game_type):
    @commands.command(name=name, help=game_type.title)
    @commands.bot_has_permissions(embed_links=True)
    async def command(self, ctx, *, elem: RPSElement(game_type)):
        if elem.element is _null_element:  # null element is a singleton
            # XXX: De-nest
            key = (ctx.invoked_with, ctx.author.id)
            if key not in __warned_about_bad_element:
                __warned_about_bad_element.add(key)
                desc = game_type.format_elements()

                embed = (discord.Embed(colour=0xFFC107, description=desc)
                         .set_author(name='Please choose an actual element')
                         .set_footer(text="If you do this again I'll win \U0001f609")
                         )
                embed.colour = 0xFFC107
                return await ctx.send(embed=embed, delete_after=90)

        await self._rps(ctx, elem, game_type)

    @command.error
    async def command_error(self, ctx, error):
        if not isinstance(error, commands.MissingRequiredArgument):
            ctx.__bypass_local_error__ = True
            return

        embed = game_type.element_embed()
        embed.description += f'\n\u200b\n(type `{ctx.clean_prefix}{ctx.invoked_with} element`)'
        return await ctx.send(embed=embed, delete_after=90)

    return command


def _create_commands():
    path = pathlib.Path('data', 'games', 'rps')
    for rps in path.glob('*.json'):
        with rps.open() as f:
            data = json.load(f)

        beats = defaultdict(set)
        outcomes = {}
        for line in data['outcomes']:
            winner, loser = map(str.lower, re.findall(r'\{(.*?)\}', line))

            outcomes[f'{winner}|{loser}'] = line
            beats[winner].add(loser)

        data['elements'].update([
            (k, Element(v, beats[k])) for k, v in data['elements'].items()
        ])

        game_type = GameType(data['title'], data['elements'], outcomes)
        setattr(RockPaperScissors, rps.stem, _make_rps_command(rps.stem, game_type))

_create_commands()
del _create_commands


def setup(bot):
    bot.add_cog(RockPaperScissors())
