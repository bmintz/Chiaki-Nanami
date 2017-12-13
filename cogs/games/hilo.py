import asyncio
import contextlib
import discord
import itertools

from discord.ext import commands
from random import choice

from .cards import Suit, Deck, images as card_images
from .manager import SessionManager

from ..utils.misc import emoji_url

from core.cog import Cog


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


EMOJI_CMPS = {
    '\N{UP-POINTING SMALL RED TRIANGLE}': 1,
    '\N{LEFT RIGHT ARROW}': 0,
    '\N{DOWN-POINTING SMALL RED TRIANGLE}': -1,
}
SUIT_EMOJI_URLS = [emoji_url(s.emoji) for s in Suit]


class HiloSession:
    def __init__(self, ctx):
        self.ctx = ctx
        self._deck = Deck()
        self._message = None
        self._display = (discord.Embed(colour=ctx.bot.colour)
                         .set_author(name='Points: 0', icon_url=ctx.author.avatar_url)
                         )

    async def _get_cmp(self):
        def check(reaction, user):
            return (reaction.message.id == self._message.id
                    and user.id == self.ctx.author.id
                    and str(reaction.emoji) in EMOJI_CMPS)

        reaction, user = await self.ctx.bot.wait_for('reaction_add', timeout=7, check=check)

        with contextlib.suppress(discord.HTTPException):
            await self._message.remove_reaction(reaction, user)

        return EMOJI_CMPS[reaction.emoji]

    async def _get_ending_text(self, points, *, default):
        connection = await self.ctx.acquire()

        # Check if it's a world record
        query = 'SELECT MAX(points) FROM hilo_games;'
        wr = await connection.fetchval(query)

        if wr is None or points > wr:
            return "World Record!"

        # Check if it's a personal best
        query = 'SELECT MAX(points) FROM hilo_games WHERE player_id = $1;'
        pb = await connection.fetchval(query, self.ctx.author.id)

        if pb is None or points > pb:
            return "New Personal Best!"

        return default

    def _display_card(self, card):
        image_url = card_images.get_card_image_url(card)
        if image_url:
            self._display.set_image(url=image_url)

    async def _loop(self):
        display = self._display
        current = self._deck.draw_one()

        self._display_card(current)
        self._message = message = await self.ctx.send(embed=display)

        for e in EMOJI_CMPS:
            await message.add_reaction(e)

        for i in itertools.count():
            try:
                success = False

                try:
                    user_cmp = await self._get_cmp()
                except asyncio.TimeoutError:
                    display.title = await self._get_ending_text(i, default="Time's up!")
                    display.colour = 0x9E9E9E
                    return i

                old, current = current, self._deck.draw_one(fill=True)
                result = _cmp(current.rank.value, old.rank.value)

                success = result == user_cmp

                if not success:
                    display.title = await self._get_ending_text(i, default="Game Over!")
                    display.colour = 0xF44336
                    return i
            finally:
                display.set_author(name=f'Points: {i + success}', icon_url=display.author.icon_url)
                self._display_card(current)

                await message.edit(embed=display)

    async def run(self):
        await self.ctx.release()
        return await self._loop()


class HigherOrLower(Cog, name='Higher or Lower?'):
    """The classic game of Higher or Lower.

    See if the next card will be... well higher or lower than the current one.
    """
    def __init__(self, bot):
        super().__init__(bot)
        self.channel_sessions = SessionManager()  # because only one game per channel
        self.user_sessions = SessionManager()     # because only one game per user

    @commands.group(invoke_without_command=True)
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
