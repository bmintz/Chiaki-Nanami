import asyncio
import asyncpg
import discord
import functools
import io
import random
import secrets
import time

from collections import namedtuple
from discord.ext import commands
from PIL import Image

from ..utils import db
from ..utils.examples import wrap_example
from ..utils.paginator import Paginator
from ..utils.misc import emoji_url


class RiggedShips(db.Table, table_name='rigged_ships'):
    id = db.Column(db.Serial, primary_key=True)
    user_id = db.Column(db.BigInt)
    partner_id = db.Column(db.BigInt)
    score = db.Column(db.SmallInt)
    comment = db.Column(db.Text, nullable=True)

    # Metadata
    guild_id = db.Column(db.BigInt)
    rigger_id = db.Column(db.BigInt)

    __create_extra__ = [
        'CHECK (user_id <= partner_id)',
        'UNIQUE (guild_id, user_id, partner_id)',
    ]

# ---------------- Ship-related utilities -------------------

def _lerp_color(c1, c2, interp):
    colors = (round((v2 - v1) * interp + v1) for v1, v2 in zip(c1, c2))
    return tuple((min(max(c, 0), 255) for c in colors))


_lerp_pink = functools.partial(_lerp_color, (0, 0, 0), (255, 105, 180))


def _user_score(user):
    return hash((
        user.id,
        user.avatar or user.default_avatar,
        str(user),
        user.display_name
    ))


_default_rating_comments = (
    'There is no chance for this to happen.',
    'Why...',
    'No way, not happening.',
    'Nope.',
    'Maybe.',
    'Woah this actually might happen.',
    'owo what\'s this',
    'You\'ve got a chance!',
    'Definitely.',
    'What are you waiting for?!',
)

_ship_heart_emojis = [
    '\N{BLACK HEART}',
    '\N{BROKEN HEART}',
    '\N{YELLOW HEART}',
    '\N{YELLOW HEART}',
    '\N{HEAVY BLACK HEART}',
    '\N{SPARKLING HEART}',
    '\N{TWO HEARTS}',
    '\N{REVOLVING HEARTS}',
]


def _scale(old_min, old_max, new_min, new_max, number):
    return ((number - old_min) / (old_max - old_min)) * (new_max - new_min) + new_min

def _clamp_scale(old_min, old_max, new_min, new_max, number):
    return min(new_max, max(_scale(old_min, old_max, new_min, new_max, number), new_min))


_value_to_index = functools.partial(_scale, 0, 100, 0, len(_default_rating_comments) - 1)
_value_to_emoji = functools.partial(_clamp_scale, 0, 100, 0, len(_ship_heart_emojis) - 1)

def _emoji_from_score(score):
    return _ship_heart_emojis[round(_value_to_emoji(score))]


class _ShipRating(namedtuple('ShipRating', 'value comment emoji')):
    __slots__ = ()

    def __new__(cls, value, comment=None):
        if not comment:
            index = round(_value_to_index(value))
            comment = _default_rating_comments[index]

        emoji = _emoji_from_score(value)
        return super().__new__(cls, value, comment, emoji)

def _splice(s1, s2):
    return s1[:len(s1) // 2] + s2[len(s2) // 2:]


def _sort_users(user1, user2):
    # We want user1 x user2 to be the same as user2 x user1,
    # but we don't want duplicated entries on the table to have
    # user, partner = user1, user2 and user, partner = user2, user1
    # as not only the table would be huge, it would also make it a nightmare
    # to make a proper query for this.
    return sorted((user1, user2), key=lambda u: u.id)


async def _get_special_pairing(user_id, partner_id, guild_id, *, connection):
    query = """SELECT score, comment FROM rigged_ships
               WHERE guild_id = $1
               AND user_id = $2
               AND partner_id = $3;
            """
    row = await connection.fetchrow(query, guild_id, user_id, partner_id)
    if row is None:
        return None

    return _ShipRating(*row)


MIN_SCORE, MAX_SCORE = -32768, 32767

def score(num):
    num = int(num)
    if num < MIN_SCORE:
        raise commands.BadArgument(f'Score should be at least {MIN_SCORE}.')
    if num > MAX_SCORE:
        raise commands.BadArgument(f'Score should be less than {MAX_SCORE}.')
    return num

@wrap_example(score)
def _score_example(ctx):
    return random.choice([0, 1, 50, 99, 100])


# List of possible ratings when someone attempts to ship themself
_self_ratings = [
    "Rip {user}, they're forever alone...",
    "Selfcest is bestest.",
]


def _calculate_rating(user1, user2):
    if user1 == user2:
        index = (_user_score(user1) + int(time.time()) // random.randint(3000, 3600)) % 2
        return _ShipRating(index * 100, _self_ratings[index].format(user=user1))

    score = (_user_score(user1) + _user_score(user2)) % 100
    return _ShipRating(score)


# --------------- End ship stuffs ---------------------

TEN_SEC_REACTION = '\N{BLACK SQUARE FOR STOP}'


class OtherStuffs:
    def __init__(self, bot):
        self.bot = bot

        self._mask = open('data/images/heart.png', 'rb')

    def __unload(self):
        self._mask.close()

    # -------------------- SHIP -------------------
    async def _load_user_avatar(self, user):
        url = user.avatar_url_as(format='png', size=512)
        async with self.bot.session.get(url) as r:
            return await r.read()

    def _create_ship_image(self, score, avatar1, avatar2):
        ava_im1 = Image.open(avatar1).convert('RGBA')
        ava_im2 = Image.open(avatar2).convert('RGBA')

        # Assume the two images are square
        size = min(ava_im1.size, ava_im2.size)
        offset = round(_scale(0, 100, size[0], 0, score))

        ava_im1.thumbnail(size)
        ava_im2.thumbnail(size)

        # paste img1 on top of img2
        newimg1 = Image.new('RGBA', size=size, color=(0, 0, 0, 0))
        newimg1.paste(ava_im2, (-offset, 0))
        newimg1.paste(ava_im1, (offset, 0))

        # paste img2 on top of img1
        newimg2 = Image.new('RGBA', size=size, color=(0, 0, 0, 0))
        newimg2.paste(ava_im1, (offset, 0))
        newimg2.paste(ava_im2, (-offset, 0))

        # blend with alpha=0.5
        im = Image.blend(newimg1, newimg2, alpha=0.6)

        mask = Image.open(self._mask).convert('L')
        mask = mask.resize(ava_im1.size, resample=Image.BILINEAR)
        im.putalpha(mask)

        f = io.BytesIO()
        im.save(f, 'png')
        f.seek(0)
        return discord.File(f, filename='test.png')

    async def _ship_image(self, score, user1, user2):
        user_avatar_data1 = io.BytesIO(await self._load_user_avatar(user1))
        user_avatar_data2 = io.BytesIO(await self._load_user_avatar(user2))
        return await self.bot.loop.run_in_executor(None, self._create_ship_image, score,
                                                   user_avatar_data1, user_avatar_data2)

    async def _get_ship_rating(self, user, partner, guild, *, connection):
        user, partner = _sort_users(user, partner)

        special_rating = await _get_special_pairing(
            user.id, partner.id, guild.id,
            connection=connection
        )
        if special_rating:
            return special_rating

        return _calculate_rating(user, partner)

    @commands.command()
    @commands.bot_has_permissions(embed_links=True, attach_files=True)
    async def ship(self, ctx, user1: discord.Member, user2: discord.Member=None):
        """Ships two users together, and scores accordingly."""
        if user2 is None:
            user1, user2 = ctx.author, user1

        score, comment, emoji = await self._get_ship_rating(
            user1, user2, ctx.guild,
            connection=ctx.db
        )

        file = await self._ship_image(score, user1, user2)
        colour = discord.Colour.from_rgb(*_lerp_pink(score / 100))
        ship_name = _splice(user1.display_name, user2.display_name)

        embed = (discord.Embed(colour=colour, description=f"{user1.mention} x {user2.mention}")
                 .set_author(name=f'Shipping: {ship_name}')
                 .add_field(name=f'Score: {score}/100 {emoji}', value=f'*{comment}*')
                 .set_image(url='attachment://test.png')
                 )
        await ctx.send(file=file, embed=embed)

    @commands.command()
    @commands.bot_has_permissions(embed_links=True)
    async def slap(self, ctx, target: discord.Member=None):
        """Slaps a user"""
        # This can be refactored somehow...
        slapper = ctx.author
        if target is None:
            msg1 = f"{slapper} is just flailing their arms around, I think."
            slaps = ["http://media.tumblr.com/tumblr_lw6rfoOq481qln7el.gif"]
            msg2 = "(Hint: specify a user.)"
        elif target.id == slapper.id:
            msg1 = f"{slapper} is slapping themself, I think."
            slaps = ["https://media.giphy.com/media/rCftUAVPLExZC/giphy.gif",
                     "https://media.giphy.com/media/EQ85WxyAAwEaQ/giphy.gif",
                     ]
            msg2 = f"I wonder why they would do that..."
        elif target.id == self.bot.user.id:
            msg1 = f"{slapper} is trying to slap me, I think."
            slaps = ["http://i.imgur.com/K420Qey.gif",
                     "https://media.giphy.com/media/iUgoB9zOO0QkU/giphy.gif",
                     "https://media.giphy.com/media/Kp4c6lf3oR7lm/giphy.gif",
                     ]
            msg2 = "(Please don't do that.)"
        else:
            slaps = ["https://media.giphy.com/media/jLeyZWgtwgr2U/giphy.gif",
                     "https://media.giphy.com/media/RXGNsyRb1hDJm/giphy.gif",
                     "https://media.giphy.com/media/zRlGxKCCkatIQ/giphy.gif",
                     "http://i.imgur.com/dzefPFL.gif",
                     "https://s-media-cache-ak0.pinimg.com/originals/fc/e1/2d/fce12d3716f05d56549cc5e05eed5a50.gif",
                     ]
            msg1 = f"{target} was slapped by {slapper}."
            msg2 = f"I wonder what {target} did to deserve such violence..."

        slap_embed = (discord.Embed(colour=self.bot.colour)
                      .set_author(name=msg1)
                      .set_image(url=random.choice(slaps))
                      .set_footer(text=msg2)
                      )
        await ctx.send(embed=slap_embed)

    @commands.command()
    # @commands.has_permissions(manage_guild=True)
    async def rig(self, ctx, user: discord.Member, partner: discord.Member, score: score, *, comment):
        """Makes `{prefix}ship`ing two users give the results *you* want."""
        user, partner = _sort_users(user, partner)

        query = """
            INSERT INTO rigged_ships (
                guild_id,
                user_id,
                partner_id,
                score,
                comment,
                rigger_id
            ) VALUES ($1, $2, $3, $4, $5, $6);
        """

        try:
            await ctx.db.execute(
                query, ctx.guild.id, user.id, partner.id,
                score, comment, ctx.author.id
            )
        except asyncpg.UniqueViolationError:
            await ctx.send('Sorry, these two have already been rigged.')
        else:
            await ctx.send(f'Done. {user} and {partner} will be lovely together. <3')

    @commands.command()
    async def unrig(self, ctx, user: discord.Member, partner: discord.Member):
        """Lets me decide what I think of two users from my own free will."""
        user, partner = _sort_users(user, partner)

        # It's expensive to do two queries but there's no easy way of
        # differentiating between the pair never being rigged and the pair
        # being rigged by someone else.
        query = """SELECT id, user_id, partner_id, rigger_id FROM rigged_ships
                   WHERE guild_id = $1
                   AND user_id = $2
                   AND partner_id = $3;
                """

        row = await ctx.db.fetchrow(query, ctx.guild.id, user.id, partner.id)
        if row is None:
            return await ctx.send('These two were never a thing...')
        elif (
            ctx.author.id not in row[1:]  # (user, partner, rigger)
            and not ctx.author.guild_permissions.administrator
        ):
            return await ctx.send("You can't ruin this beautiful ship T.T")

        query = 'DELETE FROM rigged_ships WHERE id = $1';
        await ctx.db.execute(query, row['id'])
        await ctx.send(f'Nooooooo... {user} and {partner} were made for each other! :sob:')

    @commands.command()
    async def rigs(self, ctx):
        """Shows all the ships that are "rigged"""
        query = """SELECT user_id, partner_id, score FROM rigged_ships
                   WHERE guild_id = $1
                   ORDER BY score;
                """
        records = await ctx.db.fetch(query, ctx.guild.id)
        if not records:
            entries = [
                'No one has been rigged yet.',
                f'Why not add a rig two people with `{ctx.clean_prefix}rig`?'
            ]
        else:
            entries = (
                f'{_emoji_from_score(score)} <@{user_id}> x <@{partner_id}>: **{score}%**'
                for user_id, partner_id, score in records
            )

        paginator = Paginator(ctx, entries, title='Special pairings <3')
        await paginator.interact()

    @commands.command(name='10s')
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def ten_seconds(self, ctx):
        """Starts a 10s test. How well can you judge 10 seconds?"""
        await ctx.release()

        title = f'10 Seconds Test - {ctx.author}'
        description = f'Click the {TEN_SEC_REACTION} when you think 10 second have passed'

        embed = (discord.Embed(colour=0xFFFF00, description=description)
                 .set_author(name=title, icon_url=emoji_url('\N{ALARM CLOCK}'))
                 )

        message = await ctx.send(embed=embed)
        await message.add_reaction(TEN_SEC_REACTION)

        def check(reaction, user):
            return (reaction.message.id == message.id
                    and user.id == ctx.author.id
                    and reaction.emoji == TEN_SEC_REACTION
                    )

        start = time.perf_counter()
        try:
            await ctx.bot.wait_for('reaction_add', check=check, timeout=120)
        except asyncio.TimeoutError:
            embed.colour = 0x9E9E9E
            embed.set_author(name='Took too long')
            embed.description = "It's been 2 minutes. where are you?"
            await message.edit(embed=embed)
            return

        now = time.perf_counter()
        duration = now - start

        embed.colour = 0x00FF00
        embed.description = (f'When you clicked the {TEN_SEC_REACTION} button, \n'
                             f'**{duration: .2f} seconds** have passed.')
        embed.set_author(name=f'Test completed', icon_url=embed.author.icon_url)
        embed.set_thumbnail(url=ctx.author.avatar_url)
        await message.edit(embed=embed)

    @commands.command(name='reactiontest', aliases=['reacttest'])
    async def reaction_test(self, ctx):
        """Starts a reaction test. How good are your reactions?"""
        await ctx.release()  # delaaaaaaaaays

        def check(reaction, user):
            return (reaction.message.id == message.id
                    and user.id == ctx.author.id
                    and reaction.emoji == TEN_SEC_REACTION
                    )

        async def reacted(timeout):
            try:
                await ctx.bot.wait_for('reaction_add', timeout=timeout, check=check)
            except asyncio.TimeoutError:
                return False
            return True

        description = (
            'In just a few moments, this message will turn green.\n'
            f'When that happens, click {TEN_SEC_REACTION}.\n\n'
            'In the meantime, please wait...'
        )

        embed = (discord.Embed(colour=0xFFFF00, description=description)
                 .set_author(name='Reaction Test', icon_url=emoji_url('\N{HOURGLASS}'))
                 )

        message = await ctx.send(embed=embed)
        await message.add_reaction(TEN_SEC_REACTION)
        if await reacted(random.uniform(2, 5)):
            embed.colour = 0xFF0000
            embed.description = 'You clicked it too early. Please wait next time.'
            embed.set_author(name='No cheating!', icon_url=emoji_url('\N{POUTING FACE}'))
            return await message.edit(embed=embed)

        embed.colour = 0x00FF00
        embed.set_author(name='GO!', icon_url=emoji_url('\N{ALARM CLOCK}'))
        embed.description = f'Click {TEN_SEC_REACTION}!'
        await message.edit(embed=embed)

        start = time.perf_counter()
        if not await reacted(60):
            embed.colour = 0x607d8b
            embed.description = f'Please click {TEN_SEC_REACTION} quickly next time...'
            embed.set_author(name='You took too long...')
            await message.edit(embed=embed)
            return

        end = time.perf_counter()
        embed.colour = 0x2196f3
        embed.description = f'**{end - start :.3f}** seconds'
        icon = ctx.author.avatar_url_as(static_format='png')
        embed.set_author(name="Your reaction time is...", icon_url=icon)
        await message.edit(embed=embed)

    # TODO: Make hi be an alias to this (it's an alias for ->welcome rn)
    @commands.command(hidden=True)
    async def hello(self, ctx):
        """Makes me say hello."""
        await ctx.send("Hey hey, I'm Chiaki. Say hi to MIkusaba for me <3")


def setup(bot):
    bot.add_cog(OtherStuffs(bot))
