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
from more_itertools import chunked, tail

from core.cog import Cog
from ..utils.formats import pluralize
from ..utils.misc import emoji_url, REGIONAL_INDICATORS
from ..utils.paginator import BaseReactionPaginator, page
from ..utils.time import duration_units

__schema__ = """
    CREATE TABLE IF NOT EXISTS minesweeper_games (
        id SERIAL PRIMARY KEY,
        level SMALLINT NOT NULL,
        won BOOLEAN NOT NULL,
        guild_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        played_at TIMESTAMP NOT NULL,
        time REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS minesweeper_games_time_idx ON minesweeper_games (time);
"""

class HitMine(Exception):
    def __init__(self, x, y):
        self.point = x, y
        super().__init__(f'hit a mine on {x + 1} {y + 1}')

    def __str__(self):
        x, y = self.point
        return f'You hit a mine on {ascii_uppercase[x]} {ascii_uppercase[y]}... ;-;'

class BoardCancelled(Exception):
    pass

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


class _LockedMessage:
    """Proxy message object to prevent concurrency issues when editing"""
    __slots__ = ('_message', '_lock')

    def __init__(self, message):
        self._message = message
        self._lock = asyncio.Lock()

    def __getattr__(self, attr):
        return getattr(self._message, attr)

    async def edit(self, **kwargs):
        async with self._lock:
            await self._message.edit(**kwargs)


class _MinesweeperHelp(BaseReactionPaginator):
    def __init__(self, ctx, game):
        super().__init__(ctx)
        self._game = game
        # Needed to distinguish between being stopped and letting the time run out.
        self._stopped = False

    @page('\N{INFORMATION SOURCE}')
    def default(self):
        """Reactions"""
        desc = 'The goal is to clear the board without hitting a mine.'
        instructions = 'Click one of the reactions below'

        return (discord.Embed(colour=self.colour, description=desc)
                .set_author(name='Minesweeper Help', icon_url=MINESWEEPER_ICON)
                .add_field(name=instructions, value=self.reaction_help)
                )

    @page('\N{VIDEO GAME}')
    def controls(self):
        """Controls"""
        board = self._game._board
        scheme = self._game._control_scheme
        text = textwrap.dedent(f'''
        **Type in this format:**
        ```
        column row
        ```
        Use `{scheme.x_range(board.width)}` for the column
        and `{scheme.y_range(board.height)}` for the row.
        \u200b
        To flag a tile, type `f` or `flag` after the row.
        If you're unsure about a tile, type `u` or `unsure` after the row.

        Examples: {board.examples(scheme.x, scheme.y)}
        ''')
        return (discord.Embed(colour=self.colour, description=text)
                .set_author(name='Instructions', icon_url=MINESWEEPER_ICON)
                .add_field(name='In-game Reactions', value=self._game._controller.reaction_help)
                )

    @staticmethod
    def _possible_spaces():
        number = random.randint(1, 9)
        return textwrap.dedent(f'''
        \N{BLACK LARGE SQUARE} = Empty tile, reveals numbers or other empties around it.
        {number}\u20e3 = Number of mines around it. This one has {pluralize(mine=number)}.
        \N{COLLISION SYMBOL} = BOOM! Hitting a mine will instantly end the game.
        \N{TRIANGULAR FLAG ON POST} = A flagged tile means it *might* be a mine.
        \N{BLACK QUESTION MARK ORNAMENT} = It's either a mine or not. No one's sure.
        \u200b
        ''')

    @page('\N{COLLISION SYMBOL}')
    def possible_spaces(self):
        """Tiles"""
        description = (
            'There are 5 types of tiles.\n'
            + self._possible_spaces()
        )

        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name='Tiles', icon_url=MINESWEEPER_ICON)
                )

    @page('\N{BLACK SQUARE FOR STOP}')
    async def stop(self):
        """Exit"""
        await self._game.edit(self.colour, header=self._game._header)
        self._stopped = True
        return super().stop()

    async def interact(self, *args, **kwargs):
        await super().interact(*args, **kwargs)
        if not self._stopped:
            await self._game.edit(self.colour, header=self._game._header)


class _Controller(BaseReactionPaginator):
    def __init__(self, ctx, game):
        super().__init__(ctx)
        self._game = game
        self._help_future = ctx.bot.loop.create_future()
        self._help_future.set_result(None)

    # XXX: Should probably use an asyncio.Event
    def can_poll(self):
        return self._help_future.done()

    @page('\N{INFORMATION SOURCE}')
    async def help_page(self):
        """Help"""
        if self._help_future.done():
            await self._game.edit(0x90A4AE, header='Currently on the help page...')
            paginator = _MinesweeperHelp(self.context, self._game)
            self._help_future = asyncio.ensure_future(paginator.interact(
                timeout=300, release_connection=False
            ))

    @page('\N{BLACK SQUARE FOR STOP}')
    def stop(self):
        """Quit"""
        # In case the user has the help page open when canceling it
        # (this shouldn't technically happen but this is here just in case.)
        if not self._help_future.done():
            self._help_future.cancel()

        return super().stop()

    def default(self):
        return self._game.display


def _consecutive_groups(iterable, ordering=lambda x: x):
    # Copied from more-itertools
    #
    # As this is in 4.0.0 which I have not supported *yet* due to always_iterable
    # returning an iterator rather than a tuple.
    for k, g in itertools.groupby(
        enumerate(iterable), key=lambda x: x[0] - ordering(x[1])
    ):
        yield map(itemgetter(1), g)

def _first_and_last(iterable, ordering=lambda x: x):
    return (
        (next(it), next(tail(1, it), ''))
        for it in _consecutive_groups(iterable, ordering)
    )


_numbers_to_letters_marker = object()
_possible_characters = [
    *map(str, range(1, 18)),
    _numbers_to_letters_marker,
    *ascii_lowercase
]

def _range_row(strings):
    return ' or '.join(
        (f'{start}-{end}' if end else start).upper()
        for start, end in _first_and_last(strings, _possible_characters.index)
    )


_number_emojis = [f'{i}\u20e3' for i in range(1, 10)] + ['\N{KEYCAP TEN}']

class ControlScheme(collections.namedtuple('ControlScheme', 'x y x_row y_row pattern')):
    __slots__ = ()

    def x_range(self, limit):
        return _range_row(self.x[:limit])

    def y_range(self, limit):
        return _range_row(self.y[:limit])


DEFAULT_CONTROL_SCHEME = ControlScheme(
    ascii_lowercase, [*map(str, range(1, 11)), *'abcdefgh'],
    REGIONAL_INDICATORS, [*_number_emojis, *REGIONAL_INDICATORS[:7]],
    '([a-q])\s{0,1}(10|[1-9a-g])\s{0,1}(flag|unsure|u|f)?',
)


CUSTOM_EMOJI_CONTROL_SCHEME = ControlScheme(
    ascii_lowercase, list(map(str, range(1, 18))),
    REGIONAL_INDICATORS, [],
    '([a-q])\s{0,1}(1?[0-9])\s{0,1}(flag|unsure|u|f)?',
)


class MinesweeperSession:
    def __init__(self, ctx, level, board):
        self._board = board
        self._ctx = ctx
        self._header = f'Minesweeper - {level}'
        self._controller = _Controller(ctx, self)
        self._input = None
        self._control_scheme = ctx.__msw_control_scheme__

    @property
    def display(self):
        board, ctx = self._board, self._ctx
        description = f'**Player:** {ctx.author}\n{board}'

        return (discord.Embed(colour=ctx.bot.colour, description=description)
                .set_author(name=self._header, icon_url=ctx.author.avatar_url)
                .add_field(name='Stuck?', value='For help, click \N{INFORMATION SOURCE}.')
                )

    def _check(self, message):
        if not (self._controller.can_poll()
                and message.channel == self._ctx.channel
                and message.author == self._ctx.author):
            return

        try:
            self._input = self._parse_message(message.content)
        except ValueError:
            return

        return True

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
        raise ValueError(f'bad input {string}')

    async def _loop(self):
        # TODO: Set an event and add a wait_until_ready method on the paginator
        while not self._controller._message:
            await asyncio.sleep(0)

        # Wrap message in a lock so we don't have the messages and the reactions
        # making it go all wonky.
        self._controller._message = _LockedMessage(self._controller._message)
        wait_for = self._ctx.bot.wait_for

        while not self._board.is_solved():
            try:
                message = await wait_for('message', timeout=120, check=self._check)
            except asyncio.TimeoutError:
                if self._controller.can_poll():
                    raise
                continue

            x, y, thing = self._input
            getattr(self._board, thing.value)(x, y)

            with contextlib.suppress(discord.HTTPException):
                await message.delete()

            await self._controller._message.edit(embed=self.display)

    async def edit(self, colour, header, *, icon=None, delete_after=None):
        icon = icon or self._ctx.author.avatar_url
        display = self.display.set_author(name=header, icon_url=icon)
        display.colour = colour
        await self._controller._message.edit(embed=display, delete_after=delete_after)

    async def run(self):
        f = asyncio.ensure_future(self._loop())
        tasks = [
            f,
            self._controller.interact(timeout=None, delete_after=False, release_connection=False)
        ]

        # TODO: Timing context manager?
        start = time.perf_counter()
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        end = time.perf_counter()

        for p in pending:
            p.cancel()

        edit = self.edit
        delete_edit = partial(edit, delete_after=45)

        # This can probably be moved away and cleaned up somehow but whatever
        try:
            await f
        except asyncio.CancelledError:
            # The future would be cancelled above as any pending futures would
            # be cancelled. This will only be executed if the controller had
            # finished polling, either cleanly or with error, instead of the
            # game loop.
            try:
                await next(iter(done))
            except commands.BotMissingPermissions as e:
                await self._ctx.bot_missing_perms(e.missing_perms)
            else:
                await delete_edit(0, 'Minesweeper Stopped')
            return None, -1
        except asyncio.TimeoutError:
            await delete_edit(0, 'Out of time!')
            return None, -1
        except HitMine as e:
            # Explode the first mine...
            self._board.explode(*e.point)
            await edit(0xFFFF00, header='BOOM!', icon=BOOM_ICON)
            await asyncio.sleep(random.uniform(0.5, 1))

            # Then explode all the mines
            self._board.reveal()
            await delete_edit(0xFF0000, 'Game Over!', icon=GAME_OVER_ICON)
            return False, -1
        else:
            self._board.reveal()
            await delete_edit(0x00FF00, "You're winner!", icon=SUCCESS_ICON)
            return True, end - start
        finally:
            with contextlib.suppress(BaseException):  # equivalent to bare except
                done.pop().exception()  # suppress unused task warning


class _Leaderboard(BaseReactionPaginator):
    # XXX: Should I cache this?
    def _make_page_method(emoji, difficulty):
        # Can't use partialmethod because it's a descriptor, and my page
        # decorator can't handle descriptors properly yet
        @page(emoji)
        async def get_fastest_times(self):
            embed = (discord.Embed(colour=self.colour, title=f'Minesweeper - {difficulty}')
                     .set_author(name='Fastest times', icon_url=MINESWEEPER_ICON)
                     )

            query = """SELECT user_id, time FROM minesweeper_games
                       WHERE won AND level = $1
                       ORDER BY time
                       LIMIT 10;
                    """
            records = await self.context.pool.fetch(query, difficulty.value)
            if not records:
                embed.description = 'No records, yet. \N{WINKING FACE}'
            else:
                embed.description = '\n'.join(
                    '\\' + f'\N{COLLISION SYMBOL} <@{user_id}>: {duration_units(time)}'
                    for user_id, time in records
                )

            return embed
        return get_fastest_times

    default = _make_page_method('1\u20e3', Level.easy)
    medium = _make_page_method('2\u20e3', Level.medium)
    hard = _make_page_method('3\u20e3', Level.hard)
    del _make_page_method


def not_playing_minesweeper():
    def predicate(ctx):
        channel_id = ctx.cog.sessions.get(ctx.author.id)
        if channel_id:
            raise AlreadyPlaying(f'Please finish your game in <#{channel_id}> first.')
        return True
    return commands.check(predicate)


class Minesweeper(Cog):
    def __init__(self, bot):
        super().__init__(bot)
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

        extra_text = ''
        # Check if the player broke the world record.
        if level is not Level.custom:
            await ctx.acquire()
            extra_text = await self._get_record_text(ctx.author.id, level, time, connection=ctx.db)

        description = f'{text}\n{extra_text}'
        embed = (discord.Embed(colour=0x00FF00, timestamp=datetime.utcnow(), description=description)
                 .set_author(name='A winner is you!')
                 .set_thumbnail(url=ctx.author.avatar_url)
                 )

        await ctx.send(embed=embed)

    async def _record_game(self, ctx, level, time, won):
        await ctx.acquire()
        query = """INSERT INTO minesweeper_games (level, won, guild_id, user_id, played_at, time)
                   VALUES ($1, $2, $3, $4, $5, $6);
                """
        await ctx.db.execute(
            query,
            level.value,
            won,
            ctx.guild.id,
            ctx.author.id,
            ctx.message.created_at,
            time,
        )

    async def _get_custom_board(self, ctx, message):
        # Shorthands
        wait_for = ctx.bot.wait_for
        create_task = asyncio.ensure_future

        confirm = ctx.bot.emoji_config.confirm
        str_confirm = str(confirm)
        valid_reactions = [str_confirm, '\N{BLACK SQUARE FOR STOP}']
        is_valid = frozenset(valid_reactions).__contains__

        description_format = (
            'Please type a board.\n'
            '```\n'
            '{0} x {1} ({2} mines)'
            '```\n'
            '{error}'
        )

        examples = '`10 10 99`, `10 10 90`, `5 5 1`, `12 12 100`'

        args = (0, 0, 0)
        board = None
        error = ''
        embed = (discord.Embed(colour=ctx.bot.colour)
                 .set_author(name='Custom Minesweeper', icon_url=MINESWEEPER_ICON)
                 .add_field(name='Examples', value=examples)
                 )

        # Prime the embed straight away to avoid an extra edit HTTP request
        embed.description = description_format.format(
            *args,
            error=f'**{error}**' if error else '',
        )

        try:
            # To clean up the numbers from the last message.
            await message.clear_reactions()
        except discord.HTTPException:
            # If we can't do this then we're either in a DM channel or Chiaki
            # doesn't have Manage Messages. In this case, we don't have much of
            # a choice aside from deleting and then re-sending the messages,
            # because the only alternative is to clear out all the numbers
            # individually by calling message.remove_reactions and that would
            # take a full second.
            await message.delete()
            message = await ctx.send(embed=embed)

            # Hack to make sure the old message doesn't get deleted again
            ctx.__msw_old_menu_deleted__ = True
        else:
            # The clear succeeded, which means we can edit it and change screens.
            await message.edit(embed=embed)

            # This is needed to distinguish between the message being edited and
            # the message being deleted and re-sent
            ctx.__msw_old_menu_deleted__ = False

        # XXX: Refactor
        async def put():
            await message.add_reaction(confirm)
            await message.add_reaction('\N{BLACK SQUARE FOR STOP}')
        put_future = asyncio.ensure_future(put())

        def message_check(m):
            nonlocal args
            if not (m.channel == ctx.channel and m.author == ctx.author):
                return

            try:
                width, height, mines = map(int, m.content.split(None, 3))
                args = width, height, mines
            except ValueError:
                return

            return True

        def message_future():
            return create_task(wait_for('message', check=message_check))

        def reaction_check(reaction, user):
            if not (reaction.message.id == message.id and user == ctx.author):
                return False

            emoji = str(reaction.emoji)
            if emoji == str_confirm and error:
                return False

            return is_valid(emoji)

        def reaction_future():
            return create_task(wait_for('reaction_add', check=reaction_check))

        # XXX: Future.add_done_callback?
        def reset_future(fut):
            # We do this so we don't need to cancel both futures when we reset one.
            future_makers = [message_future, reaction_future]
            index = futures.index(fut)
            futures[index] = future_makers[index]()

        futures = [message_future(), reaction_future()]

        try:
            while True:
                description = description_format.format(
                    *args,
                    error=f'**{error}**' if error else f'**Click {confirm} to play.**' if board else '',
                )
                # Only edit if there's actually a change, to save HTTP requests.
                if description != embed.description:
                    embed.colour = 0xf44336 if error else ctx.bot.colour
                    embed.description = description
                    await message.edit(embed=embed)

                done, pending = await asyncio.wait(
                    futures,
                    timeout=60,
                    return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    raise asyncio.TimeoutError

                done_future = done.pop()
                result = done_future.result()

                # Did we add a reaction?
                if not isinstance(result, discord.Message):
                    emoji = str(result[0].emoji)
                    if emoji != str(confirm):
                        raise BoardCancelled

                    if board:
                        return board
                    error = 'Enter a board first.'

                    reset_future(done_future)
                    continue

                if ctx.me.permissions_in(ctx.channel).manage_messages:
                    # Save Discord the HTTP request
                    await result.delete()

                try:
                    board = CustomizableRowBoard(
                        *args,
                        x_row=ctx.__msw_x_row__,
                        y_row=ctx.__msw_y_row__,
                    )
                except ValueError as e:
                    error = e
                else:
                    error = None
                reset_future(done_future)
        finally:
            for f in [*futures, put_future]:
                if not f.done():
                    f.cancel()

            # If clearing the reactions failed, then the old message would be
            # deleted and a new one would be sent in its place. The problem is
            # that we need to delete this message too when the user exits the
            # menu anyway. However, this would get deleted again in _get_board,
            # so we need to avoid that.
            if ctx.__msw_old_menu_deleted__:
                await message.delete()

    async def _get_world_records(self, *, connection):
        query = 'SELECT level, MIN(time) FROM minesweeper_games WHERE won GROUP BY level;'

        wrs = [0] * len(Level)
        for level, wr in await connection.fetch(query):
            wrs[level - 1] = wr
        wrs[-1] = 0  # don't include custom mode as a WR
        return wrs

    async def _get_board(self, ctx):
        emojis = [f'{l.value}\u20e3' for l in Level] + ['\N{BLACK SQUARE FOR STOP}']
        is_valid = frozenset(emojis).__contains__

        names = [l.name.title() for l in Level] + ['Exit']
        wrs = await self._get_world_records(connection=ctx.db) + [0]
        # We don't need the database for a while. Let's release while we still can.
        await ctx.release()

        description = '\n'.join(
            f'{em} = {level} {f"(WR: {wr:.2f}s)" if wr else ""}'
            for em, level, wr in zip(emojis, names, wrs)
        )
        description = f'**Choose a level below.**\n{"-" * 20}\n{description}'

        async def put(msg, ems):
            for e in ems:
                await msg.add_reaction(e)

        embed = discord.Embed(colour=ctx.bot.colour, description=description)
        embed.set_author(name="Let's play Minesweeper!", icon_url=MINESWEEPER_ICON)

        message = await ctx.send(embed=embed)
        future = asyncio.ensure_future(put(message, emojis))

        def check(reaction, user):
            return (reaction.message.id == message.id
                    and user == ctx.author
                    and is_valid(reaction.emoji))

        try:
            reaction, _ = await ctx.bot.wait_for('reaction_add', timeout=60, check=check)
            emoji = reaction.emoji
            if emoji == emojis[-1]:
                raise BoardCancelled
            elif emoji == emojis[-2]:
                return Level.custom, await self._get_custom_board(ctx, message)

            index = int(emoji[0]) - 1
            clsmethod = getattr(CustomizableRowBoard, names[index].lower())
            board = clsmethod(x_row=ctx.__msw_x_row__, y_row=ctx.__msw_y_row__)
            return Level(index + 1), board
        finally:
            # This attribute might not always be set. e.g if we didn't
            # choose custom mode.
            if not getattr(ctx, '__msw_old_menu_deleted__', None):
                with contextlib.suppress(discord.HTTPException):
                    await message.delete()

            if hasattr(ctx, '__msw_old_menu_deleted__'):
                # We don't need this attribute anymore.
                del ctx.__msw_old_menu_deleted__

            if not future.done():
                future.cancel()

    async def _do_minesweeper(self, ctx, level, board, *, record=True):
        await ctx.release()
        won, time = await MinesweeperSession(ctx, level, board).run()
        if won is None:
            return
        elif won:
            await self._say_ending_embed(ctx, level, time)

        if record:
            await self._record_game(ctx, level, time=time, won=won)

    @commands.group(aliases=['msw'], invoke_without_command=True)
    @not_playing_minesweeper()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def minesweeper(self, ctx, level: Level = None):
        """Starts a game of Minesweeper"""
        with self._create_session(ctx):
            if level is Level.custom:
                ctx.command = self.minesweeper_custom
                return await ctx.reinvoke()

            if level is None:
                try:
                    level, board = await self._get_board(ctx)
                except (asyncio.TimeoutError, BoardCancelled):
                    return
            else:
                board = getattr(CustomizableRowBoard, level.name)(
                    x_row=ctx.__msw_x_row__,
                    y_row=ctx.__msw_y_row__
                )

            await self._do_minesweeper(ctx, level, board)

    @minesweeper.command(name='custom')
    @not_playing_minesweeper()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
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
                await self._do_minesweeper(ctx, Level.custom, board, record=False)

    @minesweeper.command(name='leaderboard', aliases=['lb'])
    async def minesweeper_leaderboard(self, ctx):
        """Shows the 10 fastest times for each level of Minesweeper."""
        pages = _Leaderboard(ctx)
        await pages.interact()

def setup(bot):
    bot.add_cog(Minesweeper(bot))
