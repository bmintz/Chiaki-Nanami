import asyncqlio
import collections
import datetime
import discord
import enum
import io
import itertools
import math
import random

from discord.ext import commands
from PIL import Image

from ..tables.base import TableBase
from ..tables.currency import Currency
from ..utils.converter import union
from ..utils.formats import pluralize
from ..utils.time import duration_units

from core.cog import Cog


# Cooldown for ->daily$
DAILY_CASH_COOLDOWN_TIME = 60 * 60 * 24


class GiveEntry(TableBase, table_name='givelog'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)
    giver = asyncqlio.Column(asyncqlio.BigInt)
    recipient = asyncqlio.Column(asyncqlio.BigInt)
    amount = asyncqlio.Column(asyncqlio.Integer)
    time = asyncqlio.Column(asyncqlio.Timestamp)


# Used for the cooldown period.
# We can't use the ext.command's cooldown handler because that's
# entirely in memory. Meaning that a bot reboot would reset the cooldown
# causing infinite dailies.
class DailyCooldown(TableBase, table_name='daily_cash_cooldowns'):
    user_id = asyncqlio.Column(asyncqlio.BigInt, primary_key=True)
    latest_time = asyncqlio.Column(asyncqlio.Timestamp)


class DailyLog(TableBase):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)
    user_id = asyncqlio.Column(asyncqlio.BigInt)
    time = asyncqlio.Column(asyncqlio.Timestamp)
    # time_idx = asyncqlio.Index(time)

    amount = asyncqlio.Column(asyncqlio.BigInt)


async def _get_user_money(session, user_id):
    query = session.select.from_(Currency).where(Currency.user_id == user_id)
    return await query.first()



class Side(enum.Enum):
    heads = h = 'heads'
    tails = t = 'tails'
    # These two are commented out for now until I feel like I'm ready to
    # do these for fun. As a future reminder for myself, these are what
    # they're supposed to do.
    #
    # Edge is extremely rare, around a 0.1% chance. Getting this right
    # however will award you an extra 5x the amount you bet.
    #
    # None is for the risky, or it can also be for the indecisive. If none
    # is specified, Chiaki will pick a random side. I'm not sure whether
    # or not I should boost the reward.
    #
    # edge = e = 'edge'
    # none = n = 'none'

    def __str__(self):
        return self.value

    @classmethod
    async def convert(cls, ctx, arg):
        try:
            return cls[arg.lower()]
        except KeyError:
            raise commands.BadArgument(f'{arg} is not a valid side...')


SIDES = list(Side)[:2]
WEIGHTS = [0.4999, 0.4999, 0.0002][:2]


class _DummyUser(collections.namedtuple('_DummyUser', 'id')):
    @property
    def mention(self):
        return f'<Unknown User | ID: {self.id}>'


class NotNegative(commands.BadArgument):
    pass

def positive_int(arg):
    value = int(arg)
    if value > 0:
        return value
    raise NotNegative('expected a positive value')


class Money(Cog):
    """For all you gamblers and money-lovers, this is this cog for you!

    Other commands from other modules might starting using this
    for betting, so be on the lookout!
    """
    def __init__(self, bot):
        super().__init__(bot)

        with open('data/images/coins/heads.png', 'rb') as heads, \
             open('data/images/coins/tails.png', 'rb') as tails:
            # Force the files to be read by converting them.
            # Normally having them not read is ok but in this case it's not,
            # because we close the actual files causing them to fail
            # when we actually need these.
            #
            # Also due to the fact that we're using this over and over again
            # we need to cache these. It's a really bad idea to open these
            # in the commands due to concurrency issues and potential race
            # conditions.
            self._heads_image = Image.open(heads).convert('RGBA')
            self._tails_image = Image.open(tails).convert('RGBA')

        if len({*self._heads_image.size, *self._tails_image.size}) != 1:
            raise RuntimeError("Images must be the same size.")

    async def __error(self, ctx, error):
        if isinstance(error, NotNegative):
            await ctx.send("I'm not letting you mess up my economy \N{POUTING FACE}")

    @property
    def image_size(self):
        return self._heads_image.size[0]

    def __unload(self):
        self._heads_image.close()
        self._tails_image.close()

    @property
    def money_emoji(self):
        return self.bot.emoji_config.money

    @commands.command(aliases=['$'])
    async def cash(self, ctx, user: discord.Member = None):
        """Shows how much money you have."""
        user = user or ctx.author
        money = await _get_user_money(ctx.session, user.id)
        if not (money and money.amount):
            return await ctx.send(f'{user.mention} has nothing :frowning:')
        await ctx.send(f'{user.mention} has **{money.amount}** {self.money_emoji}!')

    @commands.command(aliases=['lb'])
    async def leaderboard(self, ctx):
        """Shows the 10 richest people"""
        query = (ctx.session.select.from_(Currency)
                 .where(Currency.amount > 0)
                 .order_by(Currency.amount, sort_order='desc')
                 .limit(10)
                 )

        get_user = ctx.bot.get_user
        records = (
            ((get_user(row.user_id) or _DummyUser(row.user_id)).mention, row.amount)
            async for row in await query.all()
        )

        # TODO: Paginate this, this might be a bad idea when the bot gets 
        #       extremely big due to memory issues as all the entries would 
        #       be stored in memory, but it should make things a little 
        #       smoother. Maybe I could chunk it?
        fields = [f'{user} with {amount}' async for user, amount in records if amount]
        embed = discord.Embed(colour=ctx.bot.colour, description='\n'.join(fields))
        await ctx.send(embed=embed)

    async def _add_money(self, session, user_id, amount):
        # Add the money to the user. We must use raw SQl because asyncqlio
        # doesn't support UPDATE SET column = expression yet.
        query = """INSERT INTO currency (user_id, amount) 
                    VALUES ({user_id}, {amount})
                    ON CONFLICT (user_id) 
                    -- currency.amount is there to prevent ambiguities.
                    DO UPDATE SET amount = currency.amount + {amount}
                """
        await session.execute(query, {'user_id': user_id, 'amount': amount})

    @commands.command()
    async def give(self, ctx, amount: positive_int, user: discord.Member):
        """Gives some of your money to another user.

        You must have at least the amount you're trying to give.
        """
        if ctx.author == user:
            return await ctx.send('Yeah... how would that work?')

        m = await _get_user_money(ctx.session, ctx.author.id)
        if not m or m.amount < amount:
            return await ctx.send("You don't have enough...")

        m.amount -= amount
        await self._add_money(ctx.session, user.id, amount)

        # Also add it to the give log. This is so we can detect someone using
        # alts to give a main account more money.
        await ctx.session.add(GiveEntry(
            giver=ctx.author.id,
            recipient=user.id,
            amount=amount,
            time=datetime.datetime.utcnow(),
        ))

        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.is_owner()
    async def award(self, ctx, amount: int, *, user: discord.User):
        """Awards some money to a user"""
        await self._add_money(ctx.session, user.id, amount)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.is_owner()
    async def take(self, ctx, amount: int, *, user: discord.User):
        """Takes some money away from a user"""
        m = await _get_user_money(ctx.session, user.id)
        if m is None:
            return await ctx.send(f"{user.mention} has no money left. "
                                  "You might be cruel, but I'm not...")

        m.amount = max(m.amount - amount, 0)
        await ctx.session.add(m)
        await ctx.send('\N{OK HAND SIGN}')

    # =============== Generic gambling commands go here ================

    # ---------- Coinflip ----------

    async def _default_flip(self, ctx):
        """Flip called with no arguments"""
        side = random.choices(SIDES, WEIGHTS)[0]
        file = discord.File(f'data/images/coins/{side}.png', 'coin.png')

        embed = (discord.Embed(colour=ctx.bot.colour, description=f'...flipped **{side}**')
                 .set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar_url)
                 .set_image(url='attachment://coin.png')
                 )

        await ctx.send(file=file, embed=embed)

    def _flip_image(self, num_sides):
        images = {
            Side.heads: self._heads_image,
            Side.tails: self._tails_image,
        }
        stats = collections.Counter()

        root = num_sides ** 0.5
        height, width = round(root), int(math.ceil(root))

        sides = (random.choices(SIDES, WEIGHTS)[0] for _ in range(num_sides))

        size = self.image_size
        image = Image.new('RGBA', (width * size, height * size))

        for i, side in enumerate(sides):
            y, x = divmod(i, width)
            image.paste(images[side], (x * size, y * size))
            stats[side] += 1

        message = ' and '.join(pluralize(**{str(side)[:-1]: n}) for side, n in stats.items())

        f = io.BytesIO()
        image.save(f, 'png')
        f.seek(0)

        return message, discord.File(f, filename='flipcoins.png')

    async def _numbered_flip(self, ctx, number):
        if number == 1:
            await self._default_flip(ctx)
        elif number > 100:
            await ctx.send("I am not flipping that many coins for you.")
        elif number <= 0:
            await ctx.send("Please tell me how that's gonna work...")
        else:
            message, file = await ctx.bot.loop.run_in_executor(None, self._flip_image, number)

            embed = (discord.Embed(colour=ctx.bot.colour, description=f'...flipped {message}')
                     .set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar_url)
                     .set_image(url='attachment://flipcoins.png')
                     )

            await ctx.send(file=file, embed=embed)

    @commands.command()
    async def flip(self, ctx, side_or_number: union(Side, int)=None, amount: positive_int = None):
        """Flips a coin.

        The first argument can either be the side (heads or tails)
        or the number of coins you want to flip. If you don't type
        anything, I will flip one coin.

        If you specify a side for the first argument, you can also
        type the amount of money you wish to bet on for this flip.
        Getting it right gives you 2.0x the money you've bet.
        """

        if side_or_number is None:
            return await self._default_flip(ctx)
        if isinstance(side_or_number, int):
            return await self._numbered_flip(ctx, side_or_number)

        side = side_or_number

        if amount is not None:
            row = await _get_user_money(ctx.session, ctx.author.id)
            if not row or amount > row.amount:
                return await ctx.send("You don't have enough...")

            row.amount -= amount
        else:
            row = None

        # The actual coin flipping. Someone help me make this more elegant.
        actual = random.choices(SIDES, WEIGHTS)[0]
        won = actual == side

        if won:
            message = 'Yay, you got it!'
            colour = 0x4CAF50
            if row:
                amount_won = amount * 2 #2 + 5 * (actual == Side.edge))
                row.amount += amount_won
                message += f'\nYou won **{amount_won}**{self.money_emoji}'
        else:
            message = "Noooooooo, you didn't get it. :("
            colour = 0xf44336
            if row:
                lost = f'**{amount}**{self.money_emoji}' if row.amount else '**everything**'
                message += f'\nYou lost {lost}.'
        
        if row: 
            await ctx.session.add(row)

        file = discord.File(f'data/images/coins/{actual}.png', 'coin.png')

        embed = (discord.Embed(colour=colour, description=message)
                 .set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar_url)
                 .set_image(url='attachment://coin.png')
                 )

        await ctx.send(file=file, embed=embed)

    @commands.command(name='daily$', aliases=['$daily'])
    async def daily_cash(self, ctx):
        """Command to give you daily cash (between 50 and 200).

        As the name suggests, you can only use this 
        command every 24 hours.
        """
        author_id = ctx.author.id
        now = ctx.message.created_at

        # Check if the person used the command within 24 hours
        query = ctx.session.select.from_(DailyCooldown).where(DailyCooldown.user_id == author_id)
        latest = await query.first()

        if latest:
            delta = (now - latest.latest_time).total_seconds()
            retry_after = DAILY_CASH_COOLDOWN_TIME - delta

            if retry_after > 0:
                return await ctx.send(
                    f"Don't be greedy... Wait at least {duration_units(retry_after)} "
                    "before doing this command again!"
                )

        # Update the cooldown
        await (ctx.session.insert.add_row(DailyCooldown(user_id=author_id, latest_time=now))
                          .on_conflict(DailyCooldown.user_id)
                          .update(DailyCooldown.latest_time)
               )

        amount = random.randint(100, 200)
        await self._add_money(ctx.session, user.id, amount)

        # Add it to the log so we can use this later.
        await ctx.session.add(DailyLog(
            user_id=ctx.author.id,
            time=now,
            amount=amount,
        ))

        await ctx.send(
            f'{ctx.author.mention}, for your daily hope you will receive '
            f'**{amount}** {self.money_emoji}! Spend them wisely!'
        )


def setup(bot):
    bot.add_cog(Money(bot))
