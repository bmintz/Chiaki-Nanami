import asyncio
import asyncqlio
import contextlib
import discord
import emoji
import heapq
import random
import time

from discord.ext import commands
from operator import attrgetter

from .manager import SessionManager

from ..utils import converter, jsonf

from core.cog import Cog


TRACK_LENGTH = 40
DEFAULT_TRACK = '-' * TRACK_LENGTH
ANIMALS = [
    '\N{TURTLE}',
    '\N{SNAIL}',
    '\N{ELEPHANT}',
    '\N{RABBIT}',
    '\N{PIG}'
]


_Table = asyncqlio.table_base()
class Racehorse(_Table, table_name='racehorses'):
    user_id = asyncqlio.Column(asyncqlio.BigInt, primary_key=True)
    # For custom horses we're gonna support custom emojis here.
    # Custom emojis are in the format <:name:id>
    # The name has a maximum length of 32 characters, while the ID is at
    # most 21 digits long. Add that to the 2 colons and 2 angle brackets
    # for a total 57 characters. But we'll go with 64 just to play it safe.
    emoji = asyncqlio.Column(asyncqlio.String(64))


async def _get_race_horse(session, member_id):
    query = session.select.from_(Racehorse).where(Racehorse.user_id == member_id)
    horse = await query.first()
    return horse.emoji if horse else None


class RacehorseEmoji(commands.Converter):
    _converter = converter.union(discord.Emoji, str)

    async def convert(self, ctx, arg):
        emoji_ = await self._converter.convert(ctx, arg)
        # XXX: The emoji library doesn't have certain emojis.
        #      So those emojis will fail. (eg :gay_pride_flag:)
        #      These special cases will have to be added as I go.
        if isinstance(emoji_, str) and emoji_ not in emoji.UNICODE_EMOJI:
            raise commands.BadArgument(f'{arg} is not a valid emoji ;-;')

        return str(emoji_)


MINIMUM_REQUIRED_MEMBERS = 2
# fields can only go up to 25
MAXIMUM_REQUIRED_MEMBERS = 25

class _RaceWaiter:
    def __init__(self, author):
        self.members = []
        self._author = author
        self._future = None
        self._full = asyncio.Event()

    async def wait(self):
        future = self._future
        if not future:
            self._future = future = asyncio.ensure_future(asyncio.wait_for(self._full.wait(), timeout=30))

        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await future

        return len(self.members) >= 2

    def close(self, member):
        if not self._future:
            return

        if self._author != member:
            return

        self._future.cancel()

    async def add_member(self, session, member):
        #if any(r.user.id == member.id for r in self.members):
        #    raise RuntimeError("You're already in the race!")

        horse = await _get_race_horse(session, member.id)
        self.members.append(Racer(member, horse))

        if len(self.members) >= MAXIMUM_REQUIRED_MEMBERS:
            self._full.set()


class Racer:
    def __init__(self, user, animal=None):
        self.animal = animal or random.choice(ANIMALS)
        self.user = user
        self.distance = 0
        self._start = self._end = time.perf_counter()

    def update(self):
        if self.is_finished():
            return

        self.distance += random.triangular(0, 10, 3)
        if self.is_finished() and self._end == self._start:
            self._end = time.perf_counter()

    def is_finished(self):
        return self.distance >= TRACK_LENGTH + 1

    @property
    def progress(self):
        buffer = DEFAULT_TRACK
        position = round(self.distance)

        finished = self.is_finished()
        end_line = "|" * (not finished)

        return f'|{buffer[:position]}{self.animal}{buffer[position:]}{end_line}'

    @property
    def position(self):
        return min(self.distance / TRACK_LENGTH * 100, 100)

    @property
    def time_taken(self):
        return self._end - self._start

class RacingSession:
    def __init__(self, ctx, players):
        self.ctx = ctx
        self.players = players
        self._winners = set()
        self._start = None
        self._track = (discord.Embed(colour=self.ctx.bot.colour)
                      .set_author(name='Race has started!')
                      .set_footer(text='Current Leader: None')
                      )

    def update_game(self):
        for player in self.players:
            player.update()

        if not self._winners:
            self._winners.update(r for r in self.players if r.is_finished())

    def _member_fields(self):
        for player in self.players:
            extra = ('\N{TROPHY}' if player in self._winners else '\N{CHEQUERED FLAG}' if player.is_finished() else '')
            yield player.user, f'{player.progress} {extra}'

    def update_current_embed(self):
        for i, (name, value) in enumerate(self._member_fields()):
            self._track.set_field_at(i, name=name, value=value, inline=False)

        if self._winners:
            self._track.set_footer(text=f'Winner: {", ".join(str(s.user) for s in self._winners)}')
        else:
            leader = max(self.players, key=attrgetter('position'))
            position = min(leader.position, 100)
            self._track.set_footer(text=f'Current Leader: {leader.user} ({position :.2f}m)')

    async def _loop(self):
        for name, value in self._member_fields():
            self._track.add_field(name=name, value=value, inline=False)

        message = await self.ctx.send(embed=self._track)

        while not self.is_completed():
            await asyncio.sleep(random.uniform(1, 3))
            self.update_game()
            self.update_current_embed()

            try:
                await message.edit(embed=self._track)
            except discord.NotFound:
                message = await self.ctx.send(embed=self._track)

    async def _display_winners(self):
        names = ['Winner', 'Runner Up', 'Third Runner Up']

        duration = time.perf_counter() - self._start
        embed = (discord.Embed(title='Results', colour=0x00FF00)
                .set_footer(text=f'Race took {duration :.2f} seconds to finish.')
                )

        # Cannot use '\N' because the medal characters don't have a name
        # I can only refer to them by their code points.
        for title, (char, racer) in zip(names, enumerate(self.top_racers(), start=0x1f947)):
            use_flag = "\N{TROPHY}" * (racer in self._winners)
            name = f'{title} {use_flag}'
            value = f'{chr(char)} {racer.animal} {racer.user}\n({racer.time_taken :.2f}s)'
            embed.add_field(name=name, value=value, inline=False)

        await self.ctx.send(embed=embed)

    async def run(self):
        self._start = time.perf_counter()
        await self._loop()
        await self._display_winners()

    def top_racers(self, n=3):
        return heapq.nsmallest(n, self.players, key=attrgetter('time_taken'))

    def is_completed(self):
        return all(r.is_finished() for r in self.players)

class Racing(Cog):
    """Be the animal you wish to beat. Wait."""
    def __init__(self, bot):
        self.bot = bot
        self.manager = SessionManager()
        self._md = self.bot.db.bind_tables(Racehorse)

    @commands.group(invoke_without_command=True)
    async def race(self, ctx):
        """Starts a race. Or if one has already started, joins one."""

        if ctx.subcommand_passed:
            # Just fail silently if someone input something like ->race Nadeko aaaa
            return

        session = self.manager.get_session(ctx.channel)
        if session is not None:
            try:
                await session.add_member(ctx.session, ctx.author)
            except AttributeError:
                # Probably the race itself
                return await ctx.send('Sorry... you were late...')
            except Exception as e:
                await ctx.send(e)
            else:
                return await ctx.send(f'Ok, {ctx.author.mention}, good luck!')

        with self.manager.temp_session(ctx.channel, _RaceWaiter(ctx.author)) as waiter:
            await waiter.add_member(ctx.session, ctx.author)
            await ctx.send(
                f'Race has started! Type `{ctx.prefix}{ctx.invoked_with}` to join! '
                f'Be quick though, you only have 30 seconds, or until {ctx.author.mention} '
                f'closes the race with `{ctx.prefix}{ctx.invoked_with} close!`'
            )

            if not await waiter.wait():
                return await ctx.send("Can't start the race. There weren't enough people. ;-;")

        with self.manager.temp_session(ctx.channel, RacingSession(ctx, waiter.members)) as inst:
            await asyncio.sleep(random.uniform(0.25, 0.75))
            await inst.run()

    @race.command(name='close')
    async def race_close(self, ctx):
        """Stops registration of a race early."""
        session = self.manager.get_session(ctx.channel)
        if session is None:
            return await ctx.send('There is no session to close, silly...')

        try:
            session.close(ctx.author)
        except AttributeError:
            return await ctx.send("Um, I don't think you can close a race that's "
                                  "running right now...")
        else:
            await ctx.send("Ok onii-chan... I've closed it now. I'll get on to starting the race...")

    @race.command(name='horse', aliases=['ride'])
    async def race_horse(self, ctx, emoji: RacehorseEmoji=None):
        """Sets your horse for the race.

        Custom emojis are allowed. But they have to be in a server that I'm in.
        """
        if not emoji:
            query = ctx.session.select.from_(Racehorse).where(Racehorse.user_id == ctx.author.id)
            selection = await query.first()

            message = (f'{selection.emoji} will be racing on your behalf, I think.'
                       if selection else
                       "You don't have a horse. I'll give you one when you race though!")
            return await ctx.send(message)

        await (ctx.session.insert.add_row(Racehorse(user_id=ctx.author.id, emoji=emoji))
                                 .on_conflict(Racehorse.user_id)
                                 .update(Racehorse.emoji)
               )
        await ctx.send(f'Ok, you can now use {emoji}')

    @race.command(name='nohorse', aliases=['noride'])
    async def race_nohorse(self, ctx):
        """Removes your custom race."""
        # Gonna do two queries for the sake of user experience/dialogue here
        query = ctx.session.select.from_(Racehorse).where(Racehorse.user_id == ctx.author.id)
        horse = await query.first()
        if not horse:
            return await ctx.send('You never had a horse...')

        await ctx.session.remove(horse)
        await ctx.send("Okai, I'll give you a horse when I can.")


def setup(bot):
    bot.add_cog(Racing(bot))