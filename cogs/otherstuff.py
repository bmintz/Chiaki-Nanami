import asyncio
import discord
import functools
import io
import itertools
import os
import random
import secrets
import time

from collections import namedtuple
from contextlib import suppress
from datetime import datetime
from discord.ext import commands
from more_itertools import always_iterable
from PIL import Image

from .utils.misc import emoji_url, load_async

from core.cog import Cog

# ---------------- Ship-related utilities -------------------

def _lerp_color(c1, c2, interp):
    colors = (round((v2 - v1) * interp + v1) for v1, v2 in zip(c1, c2))
    return tuple((min(max(c, 0), 255) for c in colors))


_lerp_pink = functools.partial(_lerp_color, (0, 0, 0), (255, 105, 180))

# Some large-ish Merseene prime cuz... idk.
_OFFSET = 2 ** 3217 - 1

# This seed is used to change the result of ->ship without having to do a
# complicated cache
_seed = 0


async def _change_ship_seed():
    global _seed
    while True:
        _seed = secrets.randbits(256)
        next_delay = random.uniform(10, 60) * 60
        await asyncio.sleep(next_delay)


def _user_score(user):
    return (user.id
            + int(user.avatar or str(user.default_avatar.value), 16)
            # 0x10FFFF is the highest Unicode can go.
            + sum(ord(c) * 0x10FFFF * i for i, c in enumerate(user.name))
            + int(user.discriminator)
            )


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

def _scale(old_min, old_max, new_min, new_max, number):
    return ((number - old_min) / (old_max - old_min)) * (new_max - new_min) + new_min

_value_to_index = functools.partial(_scale, 0, 100, 0, len(_default_rating_comments) - 1)


class _ShipRating(namedtuple('ShipRating', 'value comment')):
    __slots__ = ()

    def __new__(cls, value, comment=None):
        if comment is None:
            index = round(_value_to_index(value))
            comment = _default_rating_comments[index]
        return super().__new__(cls, value, comment)


_special_pairs = {}


def _get_special_pairing(user1, user2):
    keys = f'{user1.id}/{user2.id}', f'{user2.id}/{user1.id}'

    # Don't wanna use more_itertools.first_true because of its dumb signature
    result = next(filter(None, map(_special_pairs.get, keys)), None)
    if result is None:
        return result

    value = result.get('value', random.randrange(101))

    try:
        comment = random.choice(always_iterable(result.get('comments')))
    except IndexError:      # most likely no comment field was specified
        comment = None

    return _ShipRating(value=value, comment=comment)


# List of possible ratings when someone attempts to ship themself
_self_ratings = [
    "Rip {user}, they're forever alone...",
    "Selfcest is bestest.",
]


def _calculate_rating(user1, user2):
    if user1 == user2:
        index = _seed % 2
        return _ShipRating(index * 100, _self_ratings[index].format(user=user1))

    special = _get_special_pairing(user1, user2)
    if special:
        return special

    score = ((_user_score(user1) + _user_score(user2)) * _OFFSET + _seed) % 100
    return _ShipRating(score)

#--------------- End ship stuffs ---------------------

PRE_PING_REMARKS = [
    # 'Pinging b1nzy',
    'hacking the mainframe...',
    'We are being rate-limited.',
    'Pong?',
]

TEN_SEC_REACTION = '\N{BLACK SQUARE FOR STOP}'


class OtherStuffs(Cog):
    def __init__(self, bot):
        self.bot = bot
        self.default_time = datetime.utcnow()
        self.bot.loop.create_task(self._load())

        self._mask = open('data/images/heart.png', 'rb')
        self._future = asyncio.ensure_future(_change_ship_seed())

    def __unload(self):
        self._mask.close()
        self._future.cancel()

    async def _load(self):
        global _special_pairs
        self.copypastas = await load_async(os.path.join('data', 'copypastas.json'))

        with suppress(FileNotFoundError):
            _special_pairs = await load_async(os.path.join('data', 'pairings.json'))

    @commands.group(invoke_without_command=True, aliases=['c+v'])
    async def copypasta(self, ctx, index: int, *, name=None):
        """Returns a copypasta from an index and name"""
        copy_pasta = self.copypastas[index]
        category, copypastas = copy_pasta['category'], copy_pasta['copypastas']
        pasta = random.choice(list(copypastas.values())) if name is None else copypastas[name.title()]

        embed = discord.Embed(title=f"{category} {name}", description=pasta, colour=0x00FF00)
        await ctx.send(embed=embed)

    @copypasta.command(name="groups")
    async def copypasta_groups(self, ctx):
        pastas = itertools.starmap('`{0}.` {1}'.format, enumerate(c['category'] for c in self.copypastas))
        embed = discord.Embed(title="All the categories (and their indices)", description='\n'.join(pastas))
        await ctx.send(embed=embed)

    @copypasta.command(name="pastas")
    async def copypasta_pastas(self, ctx, index: int):
        pastas = self.copypastas[index]
        category, copypastas = pastas['category'], pastas['copypastas']
        description = '\n'.join([f'\N{BULLET} {c}' for c in copypastas])
        embed = discord.Embed(title=category, description=description)
        await ctx.send(embed=embed)

    @copypasta.error
    @copypasta_pastas.error
    async def copypasta_error(self, ctx, error):
        cause = error.__cause__
        if isinstance(cause, IndexError):
            await ctx.send(f'Index {ctx.args[2]} is out of range.')
        elif isinstance(cause, KeyError):
            await ctx.send(f"Category \"{self.copypastas[ctx.args[2]]['category']}\" "
                           f"doesn't have pasta called \"{ctx.kwargs['name']}\"")

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

    @commands.command()
    async def ship(self, ctx, user1: discord.Member, user2: discord.Member=None):
        """Ships two users together, and scores accordingly."""
        if user2 is None:
            user1, user2 = ctx.author, user1

        score, comment = _calculate_rating(user1, user2)
        file = await self._ship_image(score, user1, user2)
        colour = discord.Colour.from_rgb(*_lerp_pink(score / 100))

        embed = (discord.Embed(colour=colour, description=f"{user1.mention} x {user2.mention}")
                 .set_author(name='Shipping')
                 .add_field(name='Score', value=f'{score}/100')
                 .add_field(name='\u200b', value=f'*{comment}*', inline=False)
                 .set_image(url='attachment://test.png')
                 )
        await ctx.send(file=file, embed=embed)

    @commands.command()
    async def ping(self, ctx):
        """Your average ping command."""
        # Set the embed for the pre-ping
        clock = random.randint(0x1F550, 0x1F567)  # pick a random clock
        embed = discord.Embed(colour=0xFFC107)
        embed.set_author(name=random.choice(PRE_PING_REMARKS), icon_url=emoji_url(chr(clock)))

        # Do the classic ping
        start = time.perf_counter()     # fuck time.monotonic()
        message = await ctx.send(embed=embed)
        end = time.perf_counter()       # fuck time.monotonic()
        ms = (end - start) * 1000

        # Edit the embed to show the actual ping
        embed.colour = 0x4CAF50
        embed.set_author(name='Poing!', icon_url=emoji_url('\U0001f3d3'))
        embed.add_field(name='Latency', value=f'{ctx.bot.latency * 1000 :.0f} ms')
        embed.add_field(name='Classic', value=f'{ms :.0f} ms', inline=False)

        await message.edit(embed=embed)

    @commands.command()
    async def slap(self, ctx, target: discord.Member=None):
        """Slaps a user"""
        # This can be refactored somehow...
        slapper = ctx.author
        if target is None:
            msg1 = f"{slapper} is just flailing their arms around, I think."
            slaps = ["http://media.tumblr.com/tumblr_lw6rfoOq481qln7el.gif",
                     "http://i46.photobucket.com/albums/f104/Anime_Is_My_Anti-Drug/KururuFlail.gif",
                     ]
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
                     "https://media.giphy.com/media/MelHtIx2kmZz2/giphy.gif",
                     "https://media.giphy.com/media/147iq4Fk1IGvba/giphy.gif",
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

    @commands.command(name='10s')
    async def ten_seconds(self, ctx):
        """Starts a 10s test. How well can you judge 10 seconds?"""

        description = f'Click the {TEN_SEC_REACTION} when you think 10 second have passed'
        embed = (discord.Embed(colour=0xFFFF00, description=description)
                .set_author(name=f'10 Seconds Test - {ctx.author}', icon_url=emoji_url('\N{ALARM CLOCK}'))
                )

        message = await ctx.send(embed=embed)
        await message.add_reaction(TEN_SEC_REACTION)

        def check(reaction, user):
            return (reaction.message.id == message.id
                    and user.id == ctx.author.id
                    and reaction.emoji == TEN_SEC_REACTION
                   )

        start = time.perf_counter()
        reaction, user = await ctx.bot.wait_for('reaction_add', check=check)
        now = time.perf_counter()
        duration = now - start

        embed.colour = 0x00FF00
        embed.description = (f'When you clicked the {TEN_SEC_REACTION} button, \n'
                             f'**{duration: .2f} seconds** have passed.')
        embed.set_author(name=f'Test completed', icon_url=embed.author.icon_url)
        embed.set_thumbnail(url=ctx.author.avatar_url)
        await message.edit(embed=embed)


def setup(bot):
    bot.add_cog(OtherStuffs(bot))
