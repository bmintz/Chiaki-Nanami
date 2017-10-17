import asyncio
import contextlib
import random

from collections import deque
from discord.ext import commands

from .manager import SessionManager

from core.cog import Cog

class InvalidGameState(Exception):
    pass


class RussianRouletteSession:
    MINIMUM_PLAYERS = 2
    MAXIMUM_PLAYERS = 22

    def __init__(self, ctx):
        self.context = ctx
        self.players = deque()

        self._running = False
        self._full = asyncio.Event()
        self._required_message = f'{ctx.prefix}click'

    def add_member(self, member):
        self.players.appendleft(member)

    def add_member_checked(self, member):
        if self._running:
            raise InvalidGameState("Sorry... you were late...")
        if member in self.players:
            raise InvalidGameState(f"{member.mention}, you are already playing!")

        self.add_member(member)

        if len(self.players) >= self.MAXIMUM_PLAYERS:
            self._full.set()

    def has_enough_players(self):
        return len(self.players) >= self.MINIMUM_PLAYERS

    def _check_number_players(self):
        if not self.has_enough_players():
            raise InvalidGameState("Couldn't start Russian Roulette because "
                                   "there wasn't enough people ;-;")

    def wait_until_full(self):
        return asyncio.wait_for(self._full.wait(), timeout=15)

    async def _loop(self):
        # some local declarations to avoid excessive dot lookup.
        wait_for = self.context.bot.wait_for
        send = self.context.send
        self._running = True

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
                await send(f"{current.mention} took too long. They must've died "
                            "a long time ago, and we didn't even realize it.")
                continue

            if not random.randrange(6):
                await send(f"{current.mention} died... there's blood everywhere... "
                            "brains all over the wall")
                await asyncio.sleep(0.5)
                await send('*shudders*')
                continue

            await send(f'{current.mention} lives to see another day...')
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
    async def russian_roulette(self, ctx):
        """Starts a game of Russian Roulette"""
        session = self.manager.get_session(ctx.channel)
        if session is None:
            with self.manager.temp_session(ctx.channel, RussianRouletteSession(ctx)) as inst:
                inst.add_member(ctx.author)
                await ctx.send( 'Russian Roulette game is starting..., '
                               f'type {ctx.prefix}{ctx.invoked_with} to join')
                winner = await inst.run()
            await ctx.send(f'{winner.mention} is the lone survivor. Congratulations...')

        else:
            session.add_member_checked(ctx.author)
            await ctx.send(f'Alright {ctx.author.mention}. Good luck.')

    @russian_roulette.error
    async def rr_error(self, ctx, error):
        cause = error.__cause__
        if isinstance(cause, InvalidGameState):
            await ctx.send(str(cause))

def setup(bot):
    bot.add_cog(RussianRoulette())
