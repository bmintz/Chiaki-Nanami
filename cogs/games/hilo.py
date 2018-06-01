import asyncio
import contextlib
import discord
import itertools

from discord.ext import commands
from functools import partialmethod
from random import choice

from .cards import Suit, Deck, images as card_images
from .manager import SessionManager

from ..utils.misc import emoji_url
from ..utils.paginator import InteractiveSession, trigger


__schema__ = """
    CREATE TABLE IF NOT EXISTS hilo_games (
        id SERIAL PRIMARY KEY,
        guild_id BIGINT NOT NULL,
        player_id BIGINT NOT NULL,
        played_at TIMESTAMP NOT NULL,
        points INTEGER NOT NULL
    );
"""


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

    async def _get_ending_text(self, *, default):
        connection = await self._bot.pool.acquire()

        # Check if it's a world record
        query = 'SELECT MAX(points) FROM hilo_games;'
        wr = await connection.fetchval(query)

        if wr is None or self._score > wr:
            return "World Record!"

        # Check if it's a personal best
        query = 'SELECT MAX(points) FROM hilo_games WHERE player_id = $1;'
        pb = await connection.fetchval(query, self.context.author.id)

        if pb is None or self._score > pb:
            return "New Personal Best!"

        return default

    async def _compare(self, cmp):
        old, self._card = self._card, self._deck.draw_one(fill=True)
        result = _cmp(self._card.rank.value, old.rank.value)

        embed = self.default()

        if cmp == result:
            self._score += 1
        else:
            embed.title = await self._get_ending_text(default="Game Over!")
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
        await self.context.acquire()
        with contextlib.suppress(Exception):
            await self._message.clear_reactions()

        if not self._timed_out:
            return

        embed = self.default()
        embed.title = await self._get_ending_text(default="Time's up!")
        embed.colour = 0x9E9E9E
        await self._message.edit(embed=embed)


class HigherOrLower:
    """The classic game of Higher or Lower.

    See if the next card will be... well higher or lower than the current one.
    """
    def __init__(self, bot):
        self.bot = bot
        self.channel_sessions = SessionManager()  # because only one game per channel
        self.user_sessions = SessionManager()     # because only one game per user

    @commands.group(invoke_without_command=True)
    @commands.bot_has_permissions(embed_links=True)
    async def hilo(self, ctx):
        """Starts a game of Higher or Lower"""
        if self.channel_sessions.session_exists(ctx.channel.id):
            return await ctx.send('A game is in progress in this channel. Please wait...')
        if self.user_sessions.session_exists(ctx.author.id):
            return await ctx.send('You already have a game in progress...')

        inst = HiloSession(ctx)
        with self.channel_sessions.temp_session(ctx.channel.id, inst), \
             self.user_sessions.temp_session(ctx.author.id, inst):
            score = await inst.run()

        query = """INSERT INTO hilo_games (guild_id, player_id, played_at, points)
                   VALUES ($1, $2, $3, $4);
                """
        await ctx.db.execute(query, ctx.guild.id, ctx.author.id, ctx.message.created_at, score)

    @hilo.command(name='leaderboard', aliases=['lb'])
    async def hilo_leaderboard(self, ctx):
        """Shows the 10 highest scores in Higher or Lower"""
        query = """SELECT player_id, points
                   FROM hilo_games
                   ORDER BY points DESC
                   LIMIT 10;
                """
        records = await ctx.db.fetch(query)

        lb = '\n'.join(
            '\\' + f'\u2b50 <@{player_id}>: {points} points '
            for player_id, points in records
        )

        embed = (discord.Embed(colour=ctx.bot.colour, description=lb)
                 .set_author(name='Higher or Lower Leaderboard', icon_url=choice(SUIT_EMOJI_URLS))
                 )
        await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(HigherOrLower(bot))
