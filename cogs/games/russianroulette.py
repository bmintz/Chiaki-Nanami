import asyncio
import contextlib
import random

from collections import deque
from discord.ext import commands

from .manager import SessionManager

from ..tables.currency import Currency

from core.cog import Cog


class InvalidGameState(Exception):
    pass


class RussianRouletteSession:
    MINIMUM_PLAYERS = 2
    MAXIMUM_PLAYERS = 22

    def __init__(self, ctx):
        self.context = ctx
        self.players = deque()
        self.pot = 0

        self._full = asyncio.Event()
        self._required_message = f'{ctx.prefix}click'

    async def add_member(self, member, amount):
        if self._full.is_set():
            raise InvalidGameState("Sorry... you were late...")

        if member in self.players:
            raise InvalidGameState(f"{member.mention}, you are already playing!")

        if amount is not None:
            if amount <= 0:
                raise InvalidGameState("Yeah... no. Bet something for once!")

            async with self.context.db.get_session() as session:
                query = session.select.from_(Currency).where(Currency.user_id == member.id)
                row = await query.first()
                if not row or row.amount < amount:
                    raise InvalidGameState(f"{member.mention}, you don't have enough...")

                row.amount -= amount
                await session.add(row)
                self.pot += amount

        self.players.appendleft(member)

        if len(self.players) >= self.MAXIMUM_PLAYERS:
            self._full.set()

    def has_enough_players(self):
        return len(self.players) >= self.MINIMUM_PLAYERS

    def _check_number_players(self):
        if not self.has_enough_players():
            message = "Couldn't start Russian Roulette because there wasn't enough people ;-;"
            raise InvalidGameState(message)

    def wait_until_full(self):
        return asyncio.wait_for(self._full.wait(), timeout=15)

    async def _loop(self):
        # some local declarations to avoid excessive dot lookup.
        wait_for = self.context.bot.wait_for
        send = self.context.send
        self._full.set()

        while len(self.players) != 1:
            await asyncio.sleep(random.uniform(1, 2))
            current = self.players.popleft()

            def check(m):
                return (m.channel       == self.context.channel
                        and m.author.id == current.id
                        and m.content   == self._required_message)

            await send(f'Alright {current.mention}, it is now your turn. '
                       f'Type `{self._required_message}` to pull the trigger...')

            try:
                message = await wait_for('message', timeout=30, check=check)
            except asyncio.TimeoutError:
                await send(
                    f"{current} took too long. They must've died a long time ago, "
                    "and we didn't even realize it."
                )
                continue

            if not random.randrange(6):
                await send(
                    f"{current} died... there's blood everywhere... "
                    "brains all over the wall... *shudders*"
                )
                continue

            await send(f'{current} lives to see another day...')
            self.players.append(current)

    async def run(self):
        with contextlib.suppress(asyncio.TimeoutError):
            await self.wait_until_full()

        self._check_number_players()
        await self._loop()

        await asyncio.sleep(random.uniform(1, 2))
        return self.players.popleft()


class RussianRoulette(Cog):
    """The ultimate test of luck."""
    def __init__(self):
        self.manager = SessionManager()

    @commands.command(name='russianroulette', aliases=['rusr'])
    async def russian_roulette(self, ctx, amount: int = None):
        """Starts a game of Russian Roulette. Or joins one if one has already started.

        You can also bet money. If you're the last one standing,
        you win all the money that was bet in that game.
        """

        session = self.manager.get_session(ctx.channel)
        if session is None:
            with self.manager.temp_session(ctx.channel, RussianRouletteSession(ctx)) as inst:
                try:
                    await inst.add_member(ctx.author, amount)
                except InvalidGameState as e:
                    return await ctx.send(e)

                await ctx.send(
                    f'Russian Roulette game is starting... Type {ctx.prefix}{ctx.invoked_with} '
                    'to join! You have 15 seconds before it closes.'
                )

                try:
                    winner = await inst.run()
                except InvalidGameState as e:
                    return await ctx.send(e)

            if inst.pot:
                await (ctx.session.update.table(Currency)
                                  .set(Currency.amount + inst.pot)
                                  .where(Currency.user_id == winner.id)
                       )
                extra = f'You win **{inst.pot}**{ctx.bot.emoji_config.money}. Hope that was worth it...'
            else:
                extra = ''

            await ctx.send(f'{winner.mention} is the lone survivor. Congratulations... {extra}')

        else:
            try:
                await session.add_member(ctx.author, amount)
            except InvalidGameState as e:
                return await ctx.send(e)

            await ctx.send(f'Alright {ctx.author.mention}. Good luck.')


def setup(bot):
    bot.add_cog(RussianRoulette())
