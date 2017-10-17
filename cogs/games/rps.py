import discord
import random

from collections import namedtuple
from discord.ext import commands

from core.cog import Cog


RPS_COUNTERS = {
    "rock"     : ("paper", ),
    "paper"    : ("scissors", ),
    "scissors" : ("rock", ),
    }

RPSLS_COUNTERS = {
    "rock"     : ("paper", "spock", ),
    "paper"    : ("lizard", "scissors", ),
    "scissors" : ("spock", "rock", ),
    "lizard"   : ("scissors", "rock", ),
    "spock"    : ("paper", "lizard", ),
    }


Winner = namedtuple('Winner', 'name image')

class RockPaperScissors(Cog):
    @staticmethod
    def pick(elem, counters):
        weights = [(elem in v) * 0.5 + 0.5 for v in counters.values()]
        return random.choices(list(counters), weights)[0]

    @staticmethod
    def _cmp(elem1, elem2, counters):
        lowered1, lowered2 = elem1.lower(), elem2.lower()

        if lowered1 == lowered2:
            return 0
        # Invalid choice (according to Nadeko Nadeko wins)
        if lowered1 not in counters:
            return -1
        return 1 if lowered1 in counters[lowered2] else -1

    @staticmethod
    def _winner(res, ctx):
        if res == -1:
            return Winner("I", ctx.bot.user.avatar_url)
        elif res == 0:
            return Winner("It's a tie. No one", None)
        return Winner(ctx.author, ctx.author.avatar_url)

    async def _rps_result(self, ctx, elem, counters, *, title):
        if elem.lower() in ('chiaki', 'chiaki nanami'):
            return await ctx.send("Hey, I'm not an RPS object!")

        choice = self.pick(elem, counters)
        name, thumbnail = self._winner(self._cmp(elem, choice, counters), ctx)

        embed = (discord.Embed(colour=0x00FF00, description='\u200b')
                .set_author(name=title)
                .add_field(name=f'{ctx.author} chose...', value=f'**{elem}**', inline=False)
                .add_field(name='I chose...', value=f'**{choice.title()}**', inline=False)
                .add_field(name='Result', value=f'**{name}** wins!!', inline=False)
                )

        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        await ctx.send(embed=embed)

    @commands.command(pass_context=True)
    async def rps(self, ctx, *, elem : str):
        """Rock Paper Scissors"""
        await self._rps_result(ctx, elem, RPS_COUNTERS, title='Rock-Paper-Scissors')

    @commands.command(pass_context=True)
    async def rpsls(self, ctx, *, elem : str):
        """Rock Paper Scissors Lizard Spock"""
        await self._rps_result(ctx, elem, RPSLS_COUNTERS, title="Rock-Paper-Scissors-Lizard-Spock")


def setup(bot):
    bot.add_cog(RockPaperScissors(bot))
