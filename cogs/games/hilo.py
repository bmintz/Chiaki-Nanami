import contextlib
from functools import partialmethod
from random import choice

import discord
from discord.ext import commands

from ..utils import db
from ..utils.context_managers import temp_item
from ..utils.misc import emoji_url
from ..utils.paginator import InteractiveSession, trigger
from .cards import Deck, Suit
from .cards import images as card_images


def _cmp(a, b):
    return (a > b) - (a < b)


SUIT_EMOJI_URLS = [emoji_url(s.emoji) for s in Suit]


class HiloSession(InteractiveSession, stop_emoji=None, stop_fallback=None):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._deck = Deck()
        self._score = 0
        self._card = self._deck.draw_one()
        self._display = (discord.Embed(colour=ctx.bot.colour)
                         .set_author(name='Points: 0', icon_url=ctx.author.avatar_url)
                         )
        self._timed_out = True

    def default(self):
        image_url = card_images.get_card_image_url(self._card)
        if image_url:
            self._display.set_image(url=image_url)

        return self._display

    async def _compare(self, cmp):
        old, self._card = self._card, self._deck.draw_one(fill=True)
        result = _cmp(self._card.rank.value, old.rank.value)

        embed = self.default()

        if cmp == result:
            self._score += 1
        else:
            embed.title = "Game Over!"
            embed.colour = 0xF44336
            self._timed_out = False
            await self.stop()

        embed.set_author(name=f'Points: {self._score}', icon_url=embed.author.icon_url)
        return embed

    higher = trigger('\N{UP-POINTING SMALL RED TRIANGLE}', fallback=r'higher|h|\>', block=True)(partialmethod(_compare, 1))
    equal  = trigger('\N{LEFT RIGHT ARROW}', fallback=r'equal|e|\=', block=True)(partialmethod(_compare, 0))
    lower  = trigger('\N{DOWN-POINTING SMALL RED TRIANGLE}', fallback=r'lower|l|\<', block=True)(partialmethod(_compare, -1))

    async def run(self):
        await super().run(timeout=7)
        return self._score

    async def cleanup(self, **kwargs):
        with contextlib.suppress(Exception):
            await self._message.clear_reactions()

        if not self._timed_out:
            return

        embed = self.default()
        embed.title = "Time's up!"
        embed.colour = 0x9E9E9E
        await self._message.edit(embed=embed)


class HigherOrLower:
    """The classic game of Higher or Lower.

    See if the next card will be... well higher or lower than the current one.
    """
    def __init__(self, bot):
        self.bot = bot
        self.channel_sessions = {} # because only one game per channel
        self.user_sessions = {}    # because only one game per user

    @commands.group(invoke_without_command=True)
    @commands.bot_has_permissions(embed_links=True)
    async def hilo(self, ctx):
        """Starts a game of Higher or Lower"""
        if ctx.channel.id in self.channel_sessions:
            return await ctx.send('A game is in progress in this channel. Please wait...')
        if ctx.author.id in self.user_sessions:
            return await ctx.send('You already have a game in progress...')

        inst = HiloSession(ctx)
        with temp_item(self.channel_sessions, ctx.channel.id, inst), \
             temp_item(self.user_sessions, ctx.author.id, inst):
            score = await inst.run()


def setup(bot):
    bot.add_cog(HigherOrLower(bot))
