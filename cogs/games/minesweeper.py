import asyncio
import collections
import contextlib
import enum
import itertools
import random
import re
import textwrap
import time
from datetime import datetime
from functools import partial, partialmethod
from operator import itemgetter
from string import ascii_lowercase, ascii_uppercase

import discord
from discord.ext import commands
from more_itertools import chunked

from ..utils import db
from ..utils.formats import pluralize
from ..utils.misc import emoji_url, REGIONAL_INDICATORS
from ..utils.paginator import InteractiveSession, trigger
from ..utils.time import duration_units


class HitMine(Exception):
    def __init__(self, x, y):
        self.point = x, y
        super().__init__(f'hit a mine on {x + 1} {y + 1}')

    def __str__(self):
        x, y = self.point
        return f'You hit a mine on {ascii_uppercase[x]} {ascii_uppercase[y]}... ;-;'


class AlreadyPlaying(commands.CheckFailure):
    pass

# Some icon constants
SUCCESS_ICON = emoji_url('\N{SMILING FACE WITH SUNGLASSES}')
GAME_OVER_ICON = emoji_url('\N{DIZZY FACE}')
BOOM_ICON = emoji_url('\N{COLLISION SYMBOL}')
TIMEOUT_ICON = emoji_url('\N{ALARM CLOCK}')
# Credits to Sirea for providing the Minesweeper icon
MINESWEEPER_ICON = 'http://www.sireasgallery.com/iconset/minesweeper/Mine2_256x256_32.png'


class Level(enum.Enum):
    beginner = easy = enum.auto()
    intermediate = medium = enum.auto()
    expert = hard = enum.auto()
    custom = enum.auto()

    def __str__(self):
        return self.name.title()

    @classmethod
    async def convert(cls, ctx, arg):
        lowered = arg.lower()
        try:
            return cls[lowered]
        except KeyError:
            difficulties = '\n'.join(str(m).lower() for m in cls)
            raise commands.BadArgument(
                f'"{arg}"" is not a difficulty. Valid difficulties:\n{difficulties}'
            ) from None

    @classmethod
    def random_example(cls, ctx):
        return random.choice(list(cls._member_map_))


class FlagType(enum.Enum):
    default = 'show'
    f = 'flag'
    flag = 'flag'
    u = 'unsure'
    unsure = 'unsure'


SURROUNDING = list(filter(any, itertools.product(range(-1, 2), repeat=2)))
VISIBLE, FLAG, UNSURE, MINE = range(4)

class Board:
    def __init__(self, width, height, mines):
        self.__validate(width, height, mines)
        self.width = width
        self.height = height
        self._mine_count = mines

        self._cell_types = ({}, set(), set(), set())
        self._revealed = False
        self._explode_at = None
        self._blown_up = False

    @staticmethod
    def __validate(width, height, mines):
        if width * height >= 170:
            raise ValueError("Please make the board smaller")
        if width * height < 9:
            raise ValueError("Please make the board larger")
        if width > 17:
            raise ValueError('Please make the board thinner')
        if width < 3:
            raise ValueError('Please make the board wider')
        if height > 17:
            raise ValueError('Please make the board shorter')
        if height < 3:
            raise ValueError('Please make the board taller')
        if mines >= width * height:
            raise ValueError(f'Too many mines. Maximum is {width * height - 1}.')
        if mines <= 0:
            raise ValueError("A least one mine is required")

    def _tiles(self):
        visible, flags, unsures, mines = self._cell_types
        reveal = self._revealed
        visible_get = visible.get

        for y, x in itertools.product(range(self.height), range(self.width)):
            xy = x, y
            number = visible_get(xy)
            if number is not None:
                yield f'{number}\u20e3' if number else '\N{BLACK LARGE SQUARE}'
            elif xy in flags:
                yield '\N{TRIANGULAR FLAG ON POST}'
            elif xy in unsures:
                yield '\N{BLACK QUESTION MARK ORNAMENT}'
            elif xy in mines and reveal:
                yield '\N{COLLISION SYMBOL}' if self._blown_up else '\N{TRIANGULAR FLAG ON POST}'
            elif xy == self._explode_at:
                yield '\N{COLLISION SYMBOL}'
            else:
                yield '\N{WHITE LARGE SQUARE}'

    def __contains__(self, xy):
        x, y = xy
        return 0 <= x < self.width and 0 <= y < self.height

    def __repr__(self):
        return '{0.__class__.__name__}({0.width}, {0.height}, {0.mine_count})'.format(self)

    def __str__(self):
        meta_text = (
            f'**Marked:** {self.mines_marked} / {self.mine_count}\n'
            f'**Flags Remaining:** {self.remaining_flags}'
        )

        top_row = '\u200b'.join(REGIONAL_INDICATORS[:self.width])
        rows = map(''.join, chunked(self._tiles(), self.width))
        string = '\n'.join(map('{0}{1}'.format, REGIONAL_INDICATORS, rows))

        return f'{meta_text}\n\u200b\n\N{BLACK LARGE SQUARE}{top_row}\n{string}'

    def _place_mines_from(self, x, y):
        surrounding = set(self._get_neighbours(x, y))
        click_area = surrounding | {(x, y)}

        coords = list(itertools.filterfalse(
            click_area.__contains__,
            itertools.product(range(self.width), range(self.height))
        ))

        self.mines.update(random.sample(coords, k=min(self._mine_count, len(coords))))
        self.mines.update(random.sample(surrounding, self._mine_count - len(self.mines)))

        # All mines should be exhausted, unless we somehow made a malformed board.
        assert len(self.mines) == self._mine_count, f"only {len(self.mines)} mines were placed"

    def _is(self, type, x, y):
        return (x, y) in self._cell_types[type]

    is_mine = partialmethod(_is, MINE)
    is_visible = partialmethod(_is, VISIBLE)
    is_flag = partialmethod(_is, FLAG)
    is_unsure = partialmethod(_is, UNSURE)
    del _is

    def _get_neighbours(self, x, y):
        pairs = ((x + surr_x, y + surr_y) for (surr_x, surr_y) in SURROUNDING)
        return (p for p in pairs if p in self)

    def show(self, x, y):
        if not self.mines:
            self._place_mines_from(x, y)

        xy = x, y
        if any(xy in cells for cells in self._cell_types[:-1]):
            return

        mines, flags = self.mines, self.flags
        if xy in mines and xy not in flags:
            self._blown_up = True
            raise HitMine(x, y)

        neighbours = list(self._get_neighbours(x, y))
        surrounding = sum(n in mines for n in neighbours)
        self._cell_types[VISIBLE][xy] = surrounding

        if not surrounding:
            for nx, ny in neighbours:
                self.show(nx, ny)

    def _modify(self, type, x, y):
        if self.is_visible(x, y):
            return

        types = self._cell_types
        xy = x, y
        was_thing = xy in types[type]

        for t in [FLAG, UNSURE]:
            types[t].discard(xy)

        if not was_thing:
            types[type].add(xy)

    unsure = partialmethod(_modify, UNSURE)

    def flag(self, x, y):
        # Removing flags should still work even when you've placed the max
        # amount of flags.
        if self.is_flag(x, y) or self.remaining_flags > 0:
            self._modify(FLAG, x, y)

    def reveal(self):
        self._revealed = True

    def explode(self, x, y):
        self._explode_at = x, y

    def is_solved(self):
        return len(self.visible) + len(self.mines) == self.width * self.height

    @property
    def mines(self):
        return self._cell_types[MINE]

    @property
    def flags(self):
        return self._cell_types[FLAG]

    @property
    def visible(self):
        return self._cell_types[VISIBLE]

    @property
    def mine_count(self):
        return len(self.mines) or self._mine_count

    @property
    def mines_marked(self):
        return len(self.flags)

    @property
    def remaining_flags(self):
        return self.mine_count - self.mines_marked

    @classmethod
    def beginner(cls, **kwargs):
        """Returns a beginner minesweeper board"""
        return cls(9, 9, 10, **kwargs)

    @classmethod
    def intermediate(cls, **kwargs):
        """Returns a intermediate minesweeper board"""
        return cls(12, 12, 20, **kwargs)

    @classmethod
    def expert(cls, **kwargs):
        """Returns an expert minesweeper board"""
        return cls(13, 13, 40, **kwargs)


# Subclass that will be used for minesweeper, so that we can see
# the original board class in case we need it later.
class CustomizableRowBoard(Board):
    def __init__(self, width, height, mines, x_row, y_row):
        super().__init__(width, height, mines)
        self._x_row = x_row
        self._y_row = y_row

    def __str__(self):
        meta_text = (
            f'**Marked:** {self.mines_marked} / {self.mine_count}\n'
            f'**Flags Remaining:** {self.remaining_flags}'
        )

        top_row = '\u200b'.join(self._x_row[:self.width])
        rows = map(''.join, chunked(self._tiles(), self.width))
        string = '\n'.join(map('{0}{1}'.format, self._y_row, rows))

        return f'{meta_text}\n\u200b\n\N{BLACK LARGE SQUARE}{top_row}\n{string}'

    def examples(self, xs, ys):
        # We have duplicate values in FlagType, so we can't just iterate
        # through it normally...
        flags = list(FlagType._member_map_)[1:] + ['']
        examples = random.sample(
            list(itertools.product(range(self.width), range(self.height))), 5
        )

        random_flag = partial(random.choices, flags, weights=[.1, .1, .1, .1, .3])
        random_delim = partial(random.choices, [' ', ''], weights=[.6, .4])

        return ', '.join(
            f"**`{random_delim()[0].join((xs[x], ys[y], random_flag()[0]))}`**"
            for x, y in examples
        )


ControlScheme = collections.namedtuple('ControlScheme', 'x y x_row y_row pattern')


_number_emojis = [f'{i}\u20e3' for i in range(1, 10)] + ['\N{KEYCAP TEN}']
DEFAULT_CONTROL_SCHEME = ControlScheme(
    ascii_lowercase, [*map(str, range(1, 11)), *'abcdefgh'],
    REGIONAL_INDICATORS, [*_number_emojis, *REGIONAL_INDICATORS[:7]],
    r'([a-q])\s?(10|[1-9a-g])\s?(flag|unsure|u|f)?',
)


CUSTOM_EMOJI_CONTROL_SCHEME = ControlScheme(
    ascii_lowercase, list(map(str, range(1, 18))),
    REGIONAL_INDICATORS, [],
    r'([a-q])\s?(1?[0-9])\s?(flag|unsure|u|f)?',
)


_HELP_DESCRIPTION = '''
The goal is to clear the board without hitting a mine.

**How to Play**
Type in this format:
**`column row`**

To flag: add `f` or `flag`.
If you're unsure: add `u` or `unsure`.
Examples: {examples}

**Tiles**
\N{BLACK LARGE SQUARE} = No mines around it.
{number}\u20e3 = Number of mines around it. This one has {number_mines}.
\N{COLLISION SYMBOL} = BOOM! Hitting a mine will instantly end the game.
\N{TRIANGULAR FLAG ON POST} = Flag -- It *might* be a mine.
\N{BLACK QUESTION MARK ORNAMENT} = Either a mine or not. No one's sure.
'''


class _MinesweeperHelp(InteractiveSession, stop_fallback=None):
    def __init__(self, ctx, game):
        super().__init__(ctx)
        self._game = game
        # Needed to distinguish between being stopped and letting the time run out.
        self._stopped = False

    def default(self):
        board = self._game._board
        scheme = self._game._control_scheme
        number = random.randint(1, 9)
        description = _HELP_DESCRIPTION.format(
            examples=board.examples(scheme.x, scheme.y),
            controls=self._game.reaction_help,
            number=number,
            number_mines=pluralize(mine=number),
        )

        return (discord.Embed(colour=self._bot.colour, description=description)
                .set_author(name='Minesweeper Help', icon_url=MINESWEEPER_ICON)
                )

    @trigger('\N{BLACK SQUARE FOR STOP}', fallback='exit help')
    async def stop(self):
        """Exit"""
        await self._game.edit(self._bot.colour, header=self._game._header)
        self._stopped = True
        return await super().stop()


class _State(enum.Enum):
    NORMAL, COMPLETED, STOPPED = range(3)

class MinesweeperSession(InteractiveSession):
    def __init__(self, ctx, level, board):
        super().__init__(ctx)
        self._board = board
        self._control_scheme = ctx.__msw_control_scheme__
        self._header = f'Minesweeper - {level}'
        self._state = _State.NORMAL

        self._help_future = self._bot.loop.create_future()
        self._help_future.set_result(None)

    # Overriding paginator stuffs...
    def default(self):
        board, ctx = self._board, self.context
        description = f'**Player:** {ctx.author}\n{board}'

        if self.using_reactions():
            help_text = 'For help, click \N{INFORMATION SOURCE}'
        else:
            help_text = 'For help, type `help`'

        return (discord.Embed(colour=ctx.bot.colour, description=description)
                .set_author(name=self._header, icon_url=ctx.author.avatar_url)
                .add_field(name='Stuck?', value=help_text)
                )

    # ---------- Triggers ------------

    async def _trigger_help(self):
        cancelled = False
        try:
            await _MinesweeperHelp(self.context, self).run(timeout=300)
        except asyncio.CancelledError:
            cancelled = True
        finally:
            if not cancelled:
                await self._edit(self._bot.colour, header=self._header)

    @trigger('\N{INFORMATION SOURCE}', fallback='help')
    async def help_page(self):
        """Help"""
        if not self._help_future.done():
            return

        await self._edit(0x90A4AE, header='Currently on the help page...')
        self._help_future = self._bot.loop.create_task(self._trigger_help())

    @trigger('\N{BLACK SQUARE FOR STOP}', fallback='exit')
    async def stop(self):
        """Quit"""
        await super().stop()

        if not self._help_future.done():
            self._help_future.cancel()

        self._state = _State.STOPPED

    def __validate_input(self, x, y, flag):
        tup = x, y
        board = self._board
        if tup not in board:
            raise ValueError(f'{x} {y} is out of bounds')

        if board.is_visible(x, y):
            # Already visible, don't bother with this.
            raise ValueError(f'{x} {y} is already visible')

        if flag is FlagType.default and (board.is_flag(x, y) or board.is_unsure(x, y)):
            # We shouldn't allow exposing tiles if they're flagged or
            # marked unsure, because that doesn't make much sense.
            # If the user flagged the tile, they probably know
            # it's a mine already, and they probably don't want to step
            # on a mine they know is there.
            raise ValueError(f'{x} {y} is a flagged or unsure tile')

    def _parse(self, string):
        scheme = self._control_scheme
        match = re.fullmatch(scheme.pattern, string.lower())
        if not match:
            raise ValueError('invalid input format')

        x = scheme.x.index(match[1])
        y = scheme.y.index(match[2])
        if match[3]:
            flag = FlagType[match[3]]
        else:
            flag = FlagType.default

        self.__validate_input(x, y, flag)
        return x, y, flag

    def _legacy_parse(self, content):
        splitted = content.lower().split(None, 3)[:3]
        chars = len(splitted)

        if chars == 2:
            flag = FlagType.default
        elif chars == 3:
            flag = getattr(FlagType, splitted[2].lower(), FlagType.default)
        else:  # We need at least the x, y coordinates...
            raise ValueError(f'expected 2 or 3 tokens, got {chars}')

        x, y = map(ascii_lowercase.index, splitted[:2])
        self.__validate_input(x, y, flag)

        return x, y, flag

    def _parse_message(self, string):
        for parse in [self._parse, self._legacy_parse]:
            try:
                return parse(string)
            except ValueError:
                continue
        return None

    # InteractiveSession.run passes self, we need to ignore that.
    async def _edit_board(self, input, *_):
        x, y, thing = input
        getattr(self._board, thing.value)(x, y)

        embed = self.default()
        await self._message.edit(embed=embed)

        if not self._help_future.done():
            self._help_future.cancel()

        if self._board.is_solved():
            self._state = _State.COMPLETED
            await self._queue.put(None)

    async def on_message(self, message):
        if (
            self._blocking
            or message.channel != self._channel
            or message.author.id not in self._users
        ):
            return

        parsed = self._parse_message(message.content.lower())
        if not parsed:
            return

        await self._queue.put((partial(self._edit_board, parsed), message.delete))

    async def _edit(self, colour, header, *, icon=None, delete_after=None):
        icon = icon or self.context.author.avatar_url
        embed = self.default().set_author(name=header, icon_url=icon)
        embed.colour = colour
        await self._message.edit(embed=embed, delete_after=delete_after)

    async def run(self):
        delete_edit = partial(self._edit, delete_after=45)

        with self._bot.temp_listener(self.on_message):
            try:
                start = time.perf_counter()
                await super().run(delete_after=False, timeout=120)
            except HitMine as e:
                # Explode the first mine...
                self._board.explode(*e.point)
                await self._edit(0xFFFF00, header='BOOM!', icon=BOOM_ICON)
                await asyncio.sleep(random.uniform(0.5, 1))

                # Then explode all the mines
                self._board.reveal()
                await delete_edit(0xFF0000, 'Game Over!', icon=GAME_OVER_ICON)
                return False, -1
            else:
                state = self._state
                if state is _State.NORMAL:
                    # If this happens then run() had timed out. As of now, there's
                    # no easy way to distinguish between timeout and normal exit,
                    # so this is the simplest way.
                    await delete_edit(0, 'Out of time!')
                    return None, -1
                if state is _State.STOPPED:
                    await delete_edit(0, 'Minesweeper Stopped')
                    return None, -1

                end = time.perf_counter()
                self._board.reveal()
                await delete_edit(0x00FF00, "You're winner!", icon=SUCCESS_ICON)
                return True, end - start


_CUSTOM_DESCRIPTION_TEMPLATE = (
    'Please type a board.\n'
    '```\n'
    '{0} x {1} ({2} mines)'
    '```\n'
    '{error}'
)
_CUSTOM_EXAMPLES = '`10 10 99`, `10 10 90`, `5 5 1`, `12 12 100`'


class _MinesweeperCustomMenu(InteractiveSession):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.board = None
        self.confirmed = False
        self._args = (0, 0, 0)
        self._error = ''

    def default(self):
        if self._error:
            colour, error = 0xF44336, self._error
        else:
            colour = self._bot.colour
            error = '**Click \N{WHITE HEAVY CHECK MARK} to play.**' if self.board else ''

        description = _CUSTOM_DESCRIPTION_TEMPLATE.format(*self._args, error=error)
        colour = 0xF44336 if self._error else self._bot.colour

        return (discord.Embed(colour=colour, description=description)
                .set_author(name='Custom Minesweeper', icon_url=MINESWEEPER_ICON)
                .add_field(name='Examples', value=_CUSTOM_EXAMPLES)
                )

    async def _parse_board(self, args, _):
        self._args = args
        ctx = self.context
        try:
            board = CustomizableRowBoard(*args, x_row=ctx.__msw_x_row__, y_row=ctx.__msw_y_row__)
        except ValueError as e:
            self.board = None
            self._error = f'**{e}**'
        else:
            self._error = None
            self.board = board

        await self._message.edit(embed=self.default())

    async def on_message(self, message):
        if (
            self._blocking
            or message.channel != self._channel
            or message.author.id not in self._users
        ):
            return

        try:
            width, height, mines = map(int, message.content.split(None, 3))
            args = width, height, mines
        except ValueError:
            return

        await self._queue.put((partial(self._parse_board, args), message.delete))

    async def start(self):
        try:
            await self._message.clear_reactions()
        except:
            await super().start()
        else:
            await self._message.edit(embed=self.default())

    @trigger('\N{WHITE HEAVY CHECK MARK}', fallback='play')
    async def confirm(self):
        if self.board is not None:
            self.confirmed = True
            await self.stop()
            return

        if self._error:
            return

        error = '**Enter a board first.**'
        embed = self.default()
        embed.colour = 0xF44336
        embed.description = _CUSTOM_DESCRIPTION_TEMPLATE.format(*self._args, error=error)
        await self._message.edit(embed=embed)

    async def run(self, **kwargs):
        with self._bot.temp_listener(self.on_message):
            await super().run(**kwargs)

_CRB = CustomizableRowBoard


class _MinesweeperMenu(InteractiveSession):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.board = None
        self.level = None

    async def _get_world_records(self):
        query = 'SELECT level, MIN(time) FROM minesweeper_games WHERE won GROUP BY level;'

        wrs = [0] * len(Level)
        for level, wr in await self._bot.pool.fetch(query):
            wrs[level - 1] = wr
        wrs[-1] = 0  # don't include custom mode as a WR
        return wrs

    async def default(self):
        wrs = await self._get_world_records() + [0]
        names = [l.name.title() for l in Level] + ['Exit']

        description = '\n'.join(
            f'{em} = {level} {f"(WR: {wr:.2f}s)" if wr else ""}'
            for em, level, wr in zip(self._reaction_map, names, wrs)
        )
        description = f'**Choose a level below.**\n{"-" * 20}\n{description}'

        return (
            discord.Embed(colour=self._bot.colour, description=description)
            .set_author(name="Let's play Minesweeper!", icon_url=MINESWEEPER_ICON)
        )

    async def _set_board(self, level, clsmethod):
        ctx = self.context
        self.board = clsmethod(x_row=ctx.__msw_x_row__, y_row=ctx.__msw_y_row__)
        self.level = level
        await self.stop()

    easy   = trigger('1\u20e3', fallback='1|easy|beginner')(partialmethod(_set_board, Level.easy, _CRB.beginner))
    medium = trigger('2\u20e3', fallback='2|medium|intermediate')(partialmethod(_set_board, Level.medium, _CRB.intermediate))
    hard   = trigger('3\u20e3', fallback='3|hard|expert')(partialmethod(_set_board, Level.hard, _CRB.expert))

    @trigger('4\u20e3', fallback='4|custom', block=True)
    async def custom(self):
        custom_menu = _MinesweeperCustomMenu(self.context)
        custom_menu._message = self._message
        await custom_menu.run(timeout=60)

        if custom_menu.confirmed:
            self.board, self.level = custom_menu.board, Level.custom
        else:
            self.board = self.level = None

        await self.stop()

del _CRB


def not_playing_minesweeper():
    def predicate(ctx):
        channel_id = ctx.cog.sessions.get(ctx.author.id)
        if channel_id:
            raise AlreadyPlaying(f'Please finish your game in <#{channel_id}> first.')
        return True
    return commands.check(predicate)


class Minesweeper:
    def __init__(self, bot):
        self.bot = bot
        self.sessions = {}

    # Needed to set the "control scheme" for minesweeper.
    # Depending on whether or not the bot has access to the number emojis
    # we need to properly set the control scheme, otherwise we'll have
    # ":bad emojis:" messing up the player.
    async def __before_invoke(self, ctx):
        config = ctx.bot.emoji_config
        if ctx.bot_has_permissions(external_emojis=True) and config.msw_use_external_emojis:
            scheme = CUSTOM_EMOJI_CONTROL_SCHEME._replace(
                x_row=list(map(str, config.msw_x_row)),
                y_row=list(map(str, config.msw_y_row)),
            )
        else:
            scheme = DEFAULT_CONTROL_SCHEME

        # These don't really need to be deleted I guess, since the
        # context object doesn't last long enough to warrant that.
        ctx.__msw_control_scheme__ = scheme
        ctx.__msw_x_row__ = scheme.x_row
        ctx.__msw_y_row__ = scheme.y_row
        return ctx.__msw_control_scheme__

    async def __error(self, ctx, error):
        if isinstance(error, AlreadyPlaying):
            await ctx.send(error)

    @contextlib.contextmanager
    def _create_session(self, ctx):
        self.sessions[ctx.author.id] = ctx.channel.id
        try:
            yield
        finally:
            self.sessions.pop(ctx.author.id, None)

    async def _get_record_text(self, user_id, level, time, *, connection):
        # Check if it's the world record
        query = """SELECT MIN(time) as "world_record" FROM minesweeper_games
                   WHERE won AND level = $1;
                """
        wr = await connection.fetchval(query, level.value)

        if wr is None or time < wr:
            return "This is a new world record. Congratulations!!"

        # Check if it's a personal best
        query = """SELECT MIN(time) FROM minesweeper_games
                   WHERE won AND level = $1 AND user_id = $2;
                """
        pb = await connection.fetchval(query, level.value, user_id)

        if pb is None or time < pb:
            return "This is a new personal best!"

        return ''

    async def _say_ending_embed(self, ctx, level, time):
        rounded = round(time, 2)
        text = f'You beat Minesweeper on {level} in {duration_units(rounded)}.'

        description = text
        embed = (discord.Embed(colour=0x00FF00, timestamp=datetime.utcnow(), description=description)
                 .set_author(name='A winner is you!')
                 .set_thumbnail(url=ctx.author.avatar_url)
                 )

        await ctx.send(embed=embed)

    async def _get_board(self, ctx):
        menu = _MinesweeperMenu(ctx)
        await menu.run()
        return menu.level, menu.board

    async def _do_minesweeper(self, ctx, level, board):
        await ctx.release()
        won, time = await MinesweeperSession(ctx, level, board).run()
        if won is None:
            return
        elif won:
            await self._say_ending_embed(ctx, level, time)

    @commands.group(aliases=['msw'], invoke_without_command=True)
    @not_playing_minesweeper()
    @commands.bot_has_permissions(embed_links=True)
    async def minesweeper(self, ctx, level: Level = None):
        """Starts a game of Minesweeper"""
        with self._create_session(ctx):
            if level is Level.custom:
                ctx.command = self.minesweeper_custom
                return await ctx.reinvoke()

            if level is None:
                level, board = await self._get_board(ctx)
                if level is None:
                    return
            else:
                board = getattr(CustomizableRowBoard, level.name)(
                    x_row=ctx.__msw_x_row__,
                    y_row=ctx.__msw_y_row__
                )

            await self._do_minesweeper(ctx, level, board)

    @minesweeper.command(name='custom')
    @not_playing_minesweeper()
    @commands.bot_has_permissions(embed_links=True)
    async def minesweeper_custom(self, ctx, width: int, height: int, mines: int):
        """Starts a custom game of Minesweeper"""
        with self._create_session(ctx):
            try:
                board = CustomizableRowBoard(
                    width, height, mines,
                    ctx.__msw_x_row__, ctx.__msw_y_row__
                )
            except ValueError as e:
                await ctx.send(e)
            else:
                await self._do_minesweeper(ctx, Level.custom, board)

    @minesweeper.command(name='leaderboard', aliases=['lb'])
    async def minesweeper_leaderboard(self, ctx):
        """Shows the 10 fastest times for each level of Minesweeper."""
        pages = _Leaderboard(ctx)
        await pages.interact()

def setup(bot):
    bot.add_cog(Minesweeper(bot))
