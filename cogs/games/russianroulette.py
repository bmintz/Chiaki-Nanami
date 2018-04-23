import asyncio
import contextlib
import random

from collections import deque
from discord.ext import commands
from more_itertools import one

from .manager import SessionManager


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

    async def _update_pot(self, member, amount, *, connection):
        if amount is None:
            return

        if amount <= 0:
            raise InvalidGameState(f"How can you bet {amount} anyway?")

        currency = self.context.bot.get_cog('Money')
        if currency is None:
            raise InvalidGameState("Betting isn't available right now. Please try again later.")

        money = await currency.get_money(member.id, connection=connection)
        if money < amount:
            raise InvalidGameState(f"{member.mention}, you don't have enough...")

        await currency.add_money(member.id, -amount)
        self.pot += amount

    async def add_member(self, member, amount, *, connection):
        if self._full.is_set():
            raise InvalidGameState("Sorry... you were late...")

        if member in self.players:
            raise InvalidGameState(f"{member.mention}, you are already playing!")

        if amount is not None:
            if amount <= 0:
                raise InvalidGameState("Yeah... no. Bet something for once!")

            await self._update_pot(member, amount, connection=connection)

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
                await wait_for('message', timeout=30, check=check)
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
        await self.context.release()
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await self.wait_until_full()

            self._check_number_players()
            await self._loop()

            await asyncio.sleep(random.uniform(1, 2))
            return self.players.popleft()
        finally:
            # Regardless of whether or not we had enough players
            # we must re-acquire the connection so we can update the
            # players' amounts accordingly.
            await self.context.acquire()


class RussianRoulette:
    """The ultimate test of luck."""
    def __init__(self, bot):
        self.bot = bot
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
                    await inst.add_member(ctx.author, amount, connection=ctx.db)
                except InvalidGameState as e:
                    return await ctx.send(e)

                await ctx.send(
                    f'Russian Roulette game is starting... Type {ctx.prefix}{ctx.invoked_with} '
                    'to join! You have 15 seconds before it closes.'
                )

                try:
                    winner = await inst.run()
                except InvalidGameState as e:
                    if amount is not None:
                        currency = self.bot.get_cog('Money')
                        if currency:
                            # We can assert that there will only be one racer because there
                            # must be at least two players.
                            user = one(inst.players)
                            await currency.add_money(user.id, amount, connection=ctx.db)

                    return await ctx.send(e)

            if inst.pot:
                query = 'UPDATE currency SET amount = currency.amount + $1 WHERE user_id = $2'
                await ctx.db.execute(query, inst.pot, winner.id)
                extra = f'You win **{inst.pot}**{ctx.bot.emoji_config.money}. Hope that was worth it...'
            else:
                extra = ''

            await ctx.send(f'{winner.mention} is the lone survivor. Congratulations... {extra}')

        else:
            try:
                await session.add_member(ctx.author, amount, connection=ctx.db)
            except InvalidGameState as e:
                return await ctx.send(e)

            # Release the connection here because there's a possibility of a high
            # volume of people invoking this command. If we run into a rate-limit,
            # this can prove fatal, as Chiaki has to sleep for a certain amount
            # of time. This can cause her to hang the connection longer that she
            # needs to.
            await ctx.release()
            await ctx.send(f'Alright {ctx.author.mention}. Good luck.')


def setup(bot):
    bot.add_cog(RussianRoulette(bot))
