import collections
import discord
import enum
import io
import math
import random

from discord.ext import commands
from PIL import Image

from ..utils.converter import union
from ..utils.formats import pluralize
from ..utils.time import duration_units

from core.cog import Cog

__schema__ = """
    CREATE TABLE IF NOT EXISTS currency (
        user_id BIGINT PRIMARY KEY,
        amount INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS givelog (
        id SERIAL PRIMARY KEY,
        giver BIGINT NOT NULL,
        recipient BIGINT NOT NULL,
        amount INTEGER NOT NULL,
        time TIMESTAMP NOT NULL DEFAULT (now() at time zone 'utc')
    );

    CREATE TABLE IF NOT EXISTS daily_cash_cooldowns (
        user_id BIGINT PRIMARY KEY,
        latest_time TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS dailylog (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        time TIMESTAMP NOT NULL,
        amount INTEGER NOT NULL
    );

"""


# Cooldown for ->daily$
DAILY_CASH_COOLDOWN_TIME = 60 * 60 * 24
# minimum account age in days before one can use ->give or ->daily$
MINIMUM_ACCOUNT_AGE = 7
MINIMUM_ACCOUNT_AGE_IN_SECONDS = MINIMUM_ACCOUNT_AGE * 24 * 60 * 60


class AccountTooYoung(commands.CheckFailure):
    """Exception raised when an account is less than 7 days old."""
    pass


def maybe_not_alt():
    def predicate(ctx):
        delta = ctx.message.created_at - ctx.author.created_at
        if delta.days > MINIMUM_ACCOUNT_AGE:
            return True

        retry_after = duration_units(MINIMUM_ACCOUNT_AGE_IN_SECONDS - delta.total_seconds())
        raise AccountTooYoung(
            f"Sorry. You're too young. Wait until you're a little older "
            f"({retry_after}) before you can use `{ctx.clean_prefix}{ctx.command}`."
        )
    return commands.check(predicate)


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


class NonBlacklistedMember(commands.MemberConverter):
    async def convert(self, ctx, arg):
        member = await super().convert(ctx, arg)
        blacklist = ctx.bot.get_cog('Blacklists')

        if blacklist:
            if await blacklist.get_blacklist(member, connection=ctx.db):
                raise commands.BadArgument("This user is blacklisted.")
        return member


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
        elif isinstance(error, AccountTooYoung):
            await ctx.send(error)

    @property
    def image_size(self):
        return self._heads_image.size[0]

    def __unload(self):
        self._heads_image.close()
        self._tails_image.close()

    @property
    def money_emoji(self):
        return self.bot.emoji_config.money

    async def get_money(self, user_id, *, connection=None):
        connection = connection or self.bot.pool
        query = 'SELECT amount FROM currency WHERE user_id = $1;'
        row = await connection.fetchrow(query, user_id)
        return row['amount'] if row else 0

    async def add_money(self, user_id, amount, *, connection=None):
        connection = connection or self.bot.pool

        query = """INSERT INTO currency (user_id, amount) VALUES ($1, $2)
                   ON CONFLICT (user_id)
                   DO UPDATE SET amount = currency.amount + $2
                """
        await connection.execute(query, user_id, amount)

    @commands.command(aliases=['$'])
    async def cash(self, ctx, user: discord.Member = None):
        """Shows how much money you have."""
        user = user or ctx.author
        amount = await self.get_money(user.id, connection=ctx.db)

        if not amount:
            return await ctx.send(f'{user} has nothing :frowning:')

        await ctx.send(f'{user} has **{amount}** {self.money_emoji}!')

    @commands.command(aliases=['lb'])
    async def leaderboard(self, ctx):
        """Shows the 10 richest people"""
        query = """SELECT user_id, amount FROM currency
                   WHERE amount > 0
                   ORDER BY amount DESC
                   LIMIT 10;
                """

        get_user = ctx.bot.get_user
        fields = (
            f'{(get_user(user_id) or _DummyUser(user_id)).mention} with {amount}'
            for user_id, amount in await ctx.db.fetch(query)
        )

        # TODO: Paginate this, this might be a bad idea when the bot gets 
        #       extremely big due to memory issues as all the entries would 
        #       be stored in memory, but it should make things a little 
        #       smoother. Maybe I could chunk it?
        embed = discord.Embed(colour=ctx.bot.colour, description='\n'.join(fields))
        await ctx.send(embed=embed)

    @commands.command()
    @maybe_not_alt()
    async def give(self, ctx, amount: positive_int, user: NonBlacklistedMember):
        """Gives some of your money to another user.

        You must have at least the amount you're trying to give.
        """
        if ctx.author == user:
            return await ctx.send('Yeah... how would that work?')

        money = await self.get_money(ctx.author.id, connection=ctx.db)
        if money < amount:
            return await ctx.send("You don't have enough...")

        query = 'UPDATE currency SET amount = amount - $2 WHERE user_id = $1;'
        await ctx.db.execute(query, ctx.author.id, amount)

        await self.add_money(user.id, amount, connection=ctx.db)

        # Also add it to the give log. This is so we can detect someone using
        # alts to give a main account more money.
        query = 'INSERT INTO givelog (giver, recipient, amount) VALUES ($1, $2, $3)'
        await ctx.db.execute(query, ctx.author.id, user.id, amount)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.is_owner()
    async def award(self, ctx, amount: int, *, user: discord.User):
        """Awards some money to a user"""
        await self.add_money(user.id, amount, connection=ctx.db)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @commands.is_owner()
    async def take(self, ctx, amount: int, *, user: discord.User):
        """Takes some money away from a user"""
        money = await self.get_money(user.id, connection=ctx.db)
        if not money:
            return await ctx.send(f"{user.mention} has no money left. "
                                  "You might be cruel, but I'm not...")

        amount = min(money, amount)
        await self.add_money(user.id, -amount, connection=ctx.db)
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
        is_betting = amount is not None

        if is_betting:
            money = await self.get_money(ctx.author.id, connection=ctx.db)
            if money < amount:
                return await ctx.send("You don't have enough...")
            new_amount = -amount

        # The actual coin flipping. Someone help me make this more elegant.
        actual = random.choices(SIDES, WEIGHTS)[0]
        won = actual == side

        if won:
            message = 'Yay, you got it!'
            colour = 0x4CAF50
            if is_betting:
                new_amount = amount * 2  # 2 + 5 * (actual == Side.edge))
                message += f'\nYou won **{new_amount}**{self.money_emoji}'
        else:
            message = "Noooooooo, you didn't get it. :("
            colour = 0xf44336
            if is_betting:
                lost = '**everything**' if amount == money else f'**{amount}**{self.money_emoji}'
                message += f'\nYou lost {lost}.'

        if is_betting:
            query = 'UPDATE currency SET amount = amount + $2 WHERE user_id = $1'
            await ctx.db.execute(query, ctx.author.id, new_amount)

        file = discord.File(f'data/images/coins/{actual}.png', 'coin.png')

        embed = (discord.Embed(colour=colour, description=message)
                 .set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar_url)
                 .set_image(url='attachment://coin.png')
                 )

        await ctx.send(file=file, embed=embed)

    @commands.command(name='daily$', aliases=['$daily'])
    @maybe_not_alt()
    async def daily_cash(self, ctx):
        """Command to give you daily cash (between 50 and 200).

        As the name suggests, you can only use this
        command every 24 hours.
        """
        author_id = ctx.author.id
        now = ctx.message.created_at

        # Check if the person used the command within 24 hours
        query = 'SELECT latest_time FROM daily_cash_cooldowns WHERE user_id = $1;'
        row = await ctx.db.fetchrow(query, author_id)

        if row:
            delta = (now - row['latest_time']).total_seconds()
            retry_after = DAILY_CASH_COOLDOWN_TIME - delta

            if retry_after > 0:
                return await ctx.send(
                    f"Don't be greedy... Wait at least {duration_units(retry_after)} "
                    "before doing this command again!"
                )

        # Update the cooldown
        query = """INSERT INTO daily_cash_cooldowns (user_id, latest_time) VALUES ($1, $2)
                   ON CONFLICT (user_id)
                   DO UPDATE SET latest_time = $2;
                """
        await ctx.db.execute(query, author_id, now)

        amount = random.randint(100, 200)
        await self.add_money(author_id, amount, connection=ctx.db)

        # Add it to the daily log so we can use this later
        query = 'INSERT INTO dailylog (user_id, time, amount) VALUES ($1, $2, $3);'
        await ctx.db.execute(query, author_id, now, amount)

        await ctx.send(
            f'{ctx.author.mention}, for your daily hope you will receive '
            f'**{amount}** {self.money_emoji}! Spend them wisely!'
        )


def setup(bot):
    bot.add_cog(Money(bot))
