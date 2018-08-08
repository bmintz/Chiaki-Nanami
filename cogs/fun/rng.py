import asyncio
import collections
import colorsys
import discord
import functools
import random
import secrets
import string
import uuid

from discord.ext import commands

from ..utils import varpos
from ..utils.converter import number
from ..utils.examples import get_example, wrap_example
from ..utils.formats import escape_markdown
from ..utils.misc import emoji_url


try:
    import webcolors
except ImportError:
    webcolors = None
else:
    def _color_distance(c1, c2):
        return sum((v1 - v2) ** 2 for v1, v2 in zip(c1, c2))

    def closest_colour(requested_colour):
        min_colours = {name: _color_distance(webcolors.hex_to_rgb(key), requested_colour)
                       for key, name in webcolors.css3_hex_to_names.items()}
        return min(min_colours, key=min_colours.get)

    def get_colour_name(requested_colour):
        try:
            return webcolors.rgb_to_name(requested_colour)
        except ValueError:
            return closest_colour(requested_colour)


# TODO: Make a check file.
class PrivateMessagesOnly(commands.CheckFailure):
    pass


def dm_only():
    def predicate(ctx):
        if ctx.guild is None:
            return True
        raise PrivateMessagesOnly('This command can only be used in private messages.')
    return commands.check(predicate)


_diepio_tanks = [
    'Annihilator',
    'Assassin',
    'Auto 3',
    'Auto 5',
    'Auto Gunner',
    'Auto Smasher',
    'Auto Trapper',
    'Basic Tank',
    'Battleship',
    'Booster',
    'Destroyer',
    'Factory',
    'Fighter',
    'Flank Guard',
    'Gunner',
    'Gunner Trapper',
    'Hunter',
    'Hybrid',
    'Landmine',
    'Machine Gun',
    'Manager',
    'Mega Trapper',
    'Necromancer',
    'Octo Tank',
    'Overlord',
    'Overseer',
    'Overtrapper',
    'Pentashot',
    'Predator',
    'Quad Tank',
    'Ranger',
    'Rocketeer',
    'Skimmer',
    'Smasher',
    'Sniper',
    'Spike',
    'Sprayer',
    'Spreadshot',
    'Stalker',
    'Streamliner',
    'Trapper',
    'Tri-angle',
    'Tri-Trapper',
    'Triple Shot',
    'Triple Twin',
    'Triplet',
    'Twin',
    'Twin Flank',
]

SMASHERS = ("Auto Smasher", "Landmine", "Smasher", "Spike",)

# 8-Ball
_8BallAnswer = collections.namedtuple('_8BallAnswer', 'answer colour')
_no = functools.partial(_8BallAnswer, colour=0xf44336)
_yes = functools.partial(_8BallAnswer, colour=0x8BC34A)
_maybe = functools.partial(_8BallAnswer, colour=0xFFEB3B)
_idk = functools.partial(_8BallAnswer, colour=0)

BALL_ANSWERS = [
    _yes("Yes"),
    _no("No"),
    _maybe("Maybe so"),
    _yes("Definitely"),
    _yes("I think so"),
    _maybe("Probably"),
    _no("I don't think so"),
    _8BallAnswer("Probably not", colour=0xFF9800),
    _idk("I don't know"),
    _idk("I have no idea"),
]

_8default = _8BallAnswer('...\N{THINKING FACE}', 0x009688)

_8s = ['Eight', '8', 'Ate', 'Chiaki', '9']
_balls = ['ball', 'bool', 'bowl', 'bulli', 'smol']

def _random_8ball_name():
    eight = random.choice(_8s)
    ball = random.choices(_balls, weights=[.5] + [.1] * (len(_balls) - 1))[0]
    return eight + ball


_default_letters = string.ascii_letters + string.digits + string.punctuation


def _password(length, alphabet=_default_letters):
    return ''.join(secrets.choice(alphabet) for i in range(length))


def _make_maze(w=16, h=8):
    randrange, shuffle = random.randrange, random.shuffle
    vis = [[0] * w + [1] for _ in range(h)] + [[1] * (w + 1)]
    ver = [["|  "] * w + ['|'] for _ in range(h)] + [[]]
    hor = [["+--"] * w + ['+'] for _ in range(h + 1)]

    def walk(x, y):
        vis[y][x] = 1

        d = [(x - 1, y), (x, y + 1), (x + 1, y), (x, y - 1)]
        shuffle(d)
        for (xx, yy) in d:
            if vis[yy][xx]:
                continue
            if xx == x:
                hor[max(y, yy)][x] = "+  "
            if yy == y:
                ver[y][max(x, xx)] = "   "

            walk(xx, yy)

    walk(randrange(w), randrange(h))
    return(''.join(a + ['\n'] + b) for (a, b) in zip(hor, ver))


low_number = functools.partial(number)
@wrap_example(low_number)
def _low_number_example(ctx):
    ctx.__low_example__ = example = get_example(number, ctx)
    return example


class MaxNumber(commands.Converter):
    async def convert(self, ctx, arg):
        num = number(arg)
        if num <= ctx.args[-1]:
            raise commands.BadArgument('The second number should be higher than the first')

        return num

    @staticmethod
    def random_example(ctx):
        return ctx.__low_example__ + random.randrange(10, 51, 10)


class Choice(commands.clean_content):
    def random_example(ctx):
        try:
            choices = ctx.__choose_sample_example__
        except AttributeError:
            choices = get_example(str, ctx)
            random.shuffle(choices)
            ctx.__choose_sample_example__ = choices = iter(choices)

        return next(choices)


def diep_skill_points(arg):
    try:
        value = int(arg)
    except ValueError:
        raise commands.BadArgument(f'{arg} is not a number.')

    if not 0 < value <= 33:
        raise commands.BadArgument('Must be between 1 and 33 points')
    return value

@wrap_example(diep_skill_points)
def _diep_random_example(arg):
    return random.choice([14, 28, 33])


class RNG:
    @commands.command(name="8ball", aliases=['8'])
    @commands.bot_has_permissions(embed_links=True, attach_files=True)
    async def ball(self, ctx, *, question: str):
        """...it's a 8-ball"""
        name = _random_8ball_name()
        answer = random.choice(BALL_ANSWERS)
        description = (
            f'\N{BLACK QUESTION MARK ORNAMENT}: {question}\n'
            '\N{BILLIARDS}: {}'
        )

        embed = (discord.Embed(colour=random.randint(0, 0xFFFFFF))
                 .set_author(name=name, icon_url=emoji_url('\N{BILLIARDS}'))
                 )

        await ctx.release()

        async with ctx.typing():
            embed.description = description.format('\N{THINKING FACE}')
            msg = await ctx.send(content=ctx.author.mention, embed=embed)
            await asyncio.sleep(random.uniform(0.75, 2))

            embed.description = description.format(answer.answer)
            embed.colour = answer.colour
            await msg.edit(embed=embed)

    @varpos.require_va_command()
    async def choose(self, ctx, *choices: Choice):
        """Chooses between a list of choices.

        If one of your choices requires a space, it must be wrapped in quotes.
        """
        if len(set(choices)) < 2:
            return await ctx.send('I need more choices than that...')

        with ctx.channel.typing():
            msg = await ctx.send('\N{THINKING FACE}')
            await asyncio.sleep(random.uniform(0.25, 1))
            await msg.edit(content=random.choice(choices))

    @commands.group(aliases=['rand'], invoke_without_command=True)
    async def random(self, ctx, low: low_number, high: MaxNumber = None):
        """Gives a random number between low and high"""

        if high is None:
            if low <= 0:
                return await ctx.send('Your number should be higher than 0')
            low, high = 0, low

        if isinstance(low, int) and isinstance(high, int):
            distribution = random.randint
        else:
            distribution = random.uniform

        result = distribution(low, high)

        msg = await ctx.send(f"Your random number is...")
        await asyncio.sleep(random.uniform(0, 1))
        await msg.edit(content=msg.content + f'**{result}!!**')

    @random.command(aliases=['dice'], enabled=False)
    async def diceroll(self, ctx, amt):
        """Rolls a certain number of dice"""
        fmt = "{} " * amt
        await ctx.send(fmt.format(*[random.randint(1, 6) for _ in range(amt)]))

    # diep.io related commands

    def _build(self, points, num_stats, max_stats):
        stats = [0] * num_stats
        while points > 0:
            idx = random.randrange(num_stats)
            if stats[idx] < max_stats:
                stats[idx] += 1
                points -= 1
        return stats

    def _build_str(self, points: int=33, smasher: bool=False):
        stats = (4, 10) if smasher else (8, 7)
        return '/'.join(map(str, self._build(points, *stats)))

    @random.command()
    async def build(self, ctx, points: diep_skill_points=33):
        """Gives you a random build to try out

        If points is not provided, it defaults to a max-level build (33)"""
        await ctx.send(self._build_str(points))

    @random.command()
    async def smasher(self, ctx, points: diep_skill_points=33):
        """Gives you a random build for the Smasher branch to try out

        If points is not provided, it defaults to a max-level build (33)"""
        await ctx.send(self._build_str(points, smasher=True))

    def _class(self):
        return random.choice(_diepio_tanks)

    @random.command(name="class")
    async def class_(self, ctx):
        """Gives you a random class to play"""
        await ctx.send(self._class())

    @random.command()
    async def tank(self, ctx, points: diep_skill_points=33):
        """Gives you a random build AND class to play

        If points is not provided, it defaults to a max-level build (33)"""
        cwass = self._class()
        build = self._build_str(points, cwass in SMASHERS)
        await ctx.send(f'{build} {cwass}')

    @random.command(aliases=['color'])
    @commands.bot_has_permissions(embed_links=True)
    async def colour(self, ctx):
        """Generates a random colo(u)r."""
        colour = discord.Colour(random.randint(0, 0xFFFFFF))
        as_str = str(colour)
        rgb = colour.to_rgb()
        h, s, v = colorsys.rgb_to_hsv(*(v / 255 for v in rgb))
        hsv = h * 360, s * 100, v * 100

        colour_embed = (discord.Embed(title=as_str, colour=colour)
                        .set_thumbnail(url=f'http://colorhexa.com/{as_str[1:]}.png')
                        .add_field(name="RGB", value='%d, %d, %d' % rgb)
                        .add_field(name="HSV", value='%.03f, %.03f, %.03f' % hsv)
                        )
        if webcolors:
            colour_embed.description = get_colour_name(rgb)
        await ctx.send(embed=colour_embed)

    @commands.cooldown(rate=10, per=5, type=commands.BucketType.guild)
    @random.command()
    async def uuid(self, ctx):
        """Generates a random uuid.

        Because of potential abuse, this commands has a 5 second cooldown
        """
        await ctx.send(uuid.uuid4())

    @random.command(aliases=['pw'])
    @dm_only()
    async def password(self, ctx, n: int=8):
        """Generates a random password

        Don't worry, this uses a cryptographically secure RNG.
        However, you can only execute this in private messages
        """
        if n < 8:
            return await ctx.send(f"How can you expect a secure password in just {n} characters?")

        await ctx.send(escape_markdown(_password(n)))

    @password.error
    async def password_error(self, ctx, error):
        if isinstance(error, PrivateMessagesOnly):
            await ctx.send('Why are you asking for a password in public...?')

    @random.command()
    async def maze(self, ctx, w: int=5, h: int=5):
        """Generates a random maze"""
        if (w * 3 + 1) * (h * 2 + 1) + (h * 2) > 2000:
            return await ctx.send(f"The maze you've generated (**{w}** by **{h}**) is too large.")

        maze = '\n'.join(_make_maze(w, h))
        await ctx.send(f"```\n{maze}```")


def setup(bot):
    bot.add_cog(RNG())
