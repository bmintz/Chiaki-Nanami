import asyncio
import contextlib
import discord
import emoji
import itertools
import random
import time

from discord.ext import commands
from more_itertools import first, one, partition
from operator import attrgetter

from ..utils import converter, db, formats
from ..utils.context_managers import temp_item


class Racehorses(db.Table):
    user_id = db.Column(db.BigInt, primary_key=True)
    emoji = db.Column(db.Text)


TRACK_LENGTH = 40
DEFAULT_TRACK = '-' * TRACK_LENGTH
ANIMALS = [
    '\N{TURTLE}',
    '\N{SNAIL}',
    '\N{ELEPHANT}',
    '\N{RABBIT}',
    '\N{PIG}'
]


async def _get_race_horse(user_id, *, connection):
    query = 'SELECT emoji FROM racehorses WHERE user_id = $1;'
    row = await connection.fetchrow(query, user_id)
    return row['emoji'] if row else None


_default_emoji_examples = [
    ':thinking:',
    ':cloud_tornado:',
    ':pig2:',
    ':love_hotel:'
]

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

    @staticmethod
    def random_example(ctx):
        if random.random() > 0.5 and ctx.guild.emojis:
            return f':{random.choice(ctx.guild.emojis).name}:'
        return random.choice(_default_emoji_examples)

MINIMUM_REQUIRED_MEMBERS = 2
# fields can only go up to 25
MAXIMUM_REQUIRED_MEMBERS = 25

class _RaceWaiter:
    def __init__(self, bot, author):
        self.members = []
        self.pot = 0
        self._bot = bot
        self._author = author
        self._future = None
        self._full = asyncio.Event()

    async def wait(self):
        future = self._future
        if not future:
            self._future = future = asyncio.ensure_future(asyncio.wait_for(self._full.wait(), timeout=30))

        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await future

        return len(self.members) >= 1

    def close(self, member):
        if not self._future:
            return False

        if self._author != member:
            return False

        return self._future.cancel()

    async def _update_pot(self, member, amount, *, connection):
        if amount is None:
            return

        if amount <= 0:
            raise ValueError(f"How can you bet {amount} anyway?")

        currency = self._bot.get_cog('Money')
        if currency is None:
            raise RuntimeError("Betting isn't available right now. Please try again later.")

        money = await currency.get_money(member.id, connection=connection)
        if money < amount:
            raise RuntimeError(f"{member.mention}, you don't have enough...")

        await currency.add_money(member.id, -amount)
        self.pot += amount

    async def add_member(self, member, amount, *, connection):
        if any(r.user.id == member.id for r in self.members):
            raise RuntimeError("You're already in the race!")

        await self._update_pot(member, amount, connection=connection)
        horse = await _get_race_horse(member.id, connection=connection)
        self.members.append(Racer(member, horse))

        if len(self.members) >= MAXIMUM_REQUIRED_MEMBERS:
            self._full.set()


class Racer:
    def __init__(self, user, animal=None):
        self.animal = animal or random.choice(ANIMALS)
        self.user = user
        self.distance = 0
        self._start = self._end = None

    def update(self):
        if self._start is None:
            self._start = time.perf_counter()

        if self.is_finished():
            return

        self.distance += random.triangular(0, 10, 3)
        if self.is_finished() and self._end is None:
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
    def __init__(self, ctx, players, pot):
        self.ctx = ctx
        self.players = players
        self.pot = pot
        self._winners = set()
        self._track = (discord.Embed(colour=self.ctx.bot.colour)
                       .set_author(name='Race has started!')
                       .set_footer(text='Current Leader: None')
                       )
        if self.pot:
            self._track.description = f'Pot: **{self.pot}**{self.ctx.bot.emoji_config.money}'

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

            await asyncio.sleep(random.uniform(1, 3))

    async def _display_winners(self):
        format_racer = '{0.animal} {0.user} ({0.time_taken:.2f}s)'.format

        racers = sorted(self.players, key=attrgetter('time_taken'))
        duration = racers[-1].time_taken
        embed = (discord.Embed(title='Results', colour=0x00FF00)
                 .set_footer(text=f'Race took {duration :.2f} seconds to finish.')
                 )

        others, winners = partition(self._winners.__contains__, racers)
        winners, others = list(winners), itertools.islice(others, 2)

        # List the winners first
        name = f'Winner{"s" * (len(winners) != 1)} \N{TROPHY} '
        value = '\n'.join(map(format_racer, winners))
        embed.add_field(name=name, value=value, inline=False)

        # List the others.
        titles = ['Runner Up', 'Third Runner Up']
        medals = ['\U0001f948', '\U0001f949']

        for title, medal, racer in zip(titles, medals, others):
            embed.add_field(name=f'{title} {medal}', value=format_racer(racer), inline=False)

        if self.pot:
            if len(self._winners) == 1:
                value = f'{first(self._winners).user.mention} won **{self.pot}**!'
            else:
                value = (
                    f'{formats.human_join(w.user.mention for w in self._winners)} won '
                    f'**{self.pot // len(self._winners)}** each!'
                )

            embed.add_field(name='\u200b', value=value, inline=False)

        await self.ctx.send(embed=embed)

    async def _give_to_winners(self):
        num_winners = len(self._winners)
        amount = self.pot // num_winners
        ids = [winner.user.id for winner in self._winners]

        query = """UPDATE currency SET amount = currency.amount + $1 
                   WHERE user_id = ANY($2::BIGINT[])
                """
        await self.ctx.db.execute(query, amount, ids)

    async def run(self):
        await self._loop()
        await self._display_winners()

        if self.pot:
            await self.ctx.acquire()
            await self._give_to_winners()

    def is_completed(self):
        return all(r.is_finished() for r in self.players)

class Racing:
    """Be the animal you wish to beat. Wait."""
    def __init__(self, bot):
        self.bot = bot
        self.sessions = {}

    @commands.group(invoke_without_command=True)
    @commands.bot_has_permissions(embed_links=True)
    async def race(self, ctx, amount: int = None):
        """Starts a race. Or if one has already started, joins one.

        You can also bet some money. If you win, you will receive all the money.
        """

        session = self.sessions.get(ctx.channel.id)
        if session is not None:
            try:
                await session.add_member(ctx.author, amount, connection=ctx.db)
            except AttributeError:
                # Probably the race itself
                return await ctx.send('Sorry... you were late...')
            except Exception as e:
                return await ctx.send(e)
            else:
                # Release the connection here because there's a possibility of a high
                # volume of people invoking this command. If we run into a rate-limit,
                # this can prove fatal, as Chiaki has to sleep for a certain amount
                # of time. This can cause her to hang the connection longer that she
                # needs to.
                await ctx.release()
                return await ctx.send(f'Ok, {ctx.author.mention}, good luck!')

        with temp_item(self.sessions, ctx.channel.id, _RaceWaiter(ctx.bot, ctx.author)) as waiter:
            try:
                await waiter.add_member(ctx.author, amount, connection=ctx.db)
            except Exception as e:
                return await ctx.send(e)

            await ctx.send(
                f'Race has started! Type `{ctx.prefix}{ctx.invoked_with}` to join! '
                f'Be quick though, you only have 30 seconds, or until {ctx.author.mention} '
                f'closes the race with `{ctx.prefix}{ctx.invoked_with} close`!'
            )

            await ctx.release()  # release as we're gonna be waiting for a bit
            if not await waiter.wait():
                if amount is not None:
                    currency = self.bot.get_cog('Money')
                    if currency:
                        # We can assert that there will only be one racer because there
                        # must be at least two racers.
                        user = one(waiter.members).user
                        await ctx.acquire()
                        await currency.add_money(user.id, amount, connection=ctx.db)

                return await ctx.send("Can't start the race. There weren't enough people. ;-;")

        with temp_item(self.sessions, ctx.channel.id, RacingSession(ctx, waiter.members, waiter.pot)) as inst:
            await asyncio.sleep(random.uniform(0.25, 0.75))
            await inst.run()

    @race.command(name='close')
    async def race_close(self, ctx):
        """Stops registration of a race early."""
        session = self.sessions.get(ctx.channel.id)
        if session is None:
            return await ctx.send('There is no session to close, silly...')

        try:
            success = session.close(ctx.author)
        except AttributeError:
            return await ctx.send("Um, I don't think you can close a race that's "
                                  "running right now...")
        else:
            if success:
                await ctx.send("Ok onii-chan... I've closed it now. I'll get on to starting the race...")

    @race.command(name='horse', aliases=['ride'])
    async def race_horse(self, ctx, emoji: RacehorseEmoji=None):
        """Sets your horse for the race.

        Custom emojis are allowed. But they have to be in a server that I'm in.
        """
        if not emoji:
            emoji = await _get_race_horse(ctx.author.id, connection=ctx.db)

            message = (f'{emoji} will be racing on your behalf, I think.'
                       if emoji else
                       "You don't have a horse. I'll give you one when you race though!")
            return await ctx.send(message)

        query = """INSERT INTO racehorses (user_id, emoji) VALUES ($1, $2)
                   ON CONFLICT (user_id)
                   DO UPDATE SET emoji = $2;
                """
        await ctx.db.execute(query, ctx.author.id, emoji)
        await ctx.send(f'Ok, you can now use {emoji}')

    @race.command(name='nohorse', aliases=['noride'])
    async def race_nohorse(self, ctx):
        """Removes your custom race."""
        # Gonna do two queries for the sake of user experience/dialogue here
        query = 'DELETE FROM racehorses WHERE user_id = $1;'
        status = await ctx.db.execute(query, ctx.author.id)

        if status[-1] == '0':
            await ctx.send('You never had a horse...')
        else:
            await ctx.send("Okai, I'll give you a horse when I can.")


def setup(bot):
    bot.add_cog(Racing(bot))