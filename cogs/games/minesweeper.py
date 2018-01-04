import asyncio
import contextlib
import enum
import itertools
import random
import textwrap
import time
from datetime import datetime
from functools import partial
from string import ascii_lowercase, ascii_uppercase

import discord
from discord.ext import commands

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

class AlreadyPlaying(commands.CheckFailure):
    pass


# Some icon constants
SUCCESS_ICON = emoji_url('\N{SMILING FACE WITH SUNGLASSES}')
GAME_OVER_ICON = emoji_url('\N{DIZZY FACE}')
BOOM_ICON = emoji_url('\N{COLLISION SYMBOL}')
TIMEOUT_ICON = emoji_url('\N{ALARM CLOCK}')


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

class FlagType(enum.Enum):
    default = 'show'
    f = 'flag'
    flag = 'flag'
    u = 'unsure'
    unsure = 'unsure'


class Tile(enum.Enum):
    blank = '\N{WHITE LARGE SQUARE}'
    flag = '\N{TRIANGULAR FLAG ON POST}'
    mine = '\N{EIGHT POINTED BLACK STAR}'
    shown = '\N{BLACK LARGE SQUARE}'
    unsure = '\N{BLACK QUESTION MARK ORNAMENT}'
    boom = '\N{COLLISION SYMBOL}'

    def __str__(self):
        return self.value

    @staticmethod
    def numbered(number):
        return f'{number}\U000020e3'

SURROUNDING = list(filter(any, itertools.product(range(-1, 2), repeat=2)))

# XXX: Simplify this class later
class Board:
    def __init__(self, width, height, mines):
        self.__validate(width, height, mines)
        self._mine_count = mines

        self._board = [[Tile.blank] * width for _ in range(height)]
        self.visible = set()
        self.flags = set()
        self.unsures = set()
        self.mines = set()

    @staticmethod
    def __validate(width, height, mines):
        if width * height >= 170:
            raise ValueError("Please make the board smaller.")
        if width * height < 9:
            raise ValueError("Please make the board larger.")
        if mines >= width * height:
            raise ValueError(f'Too many mines (expected max {width * height}, got {mines})')
        if mines <= 0:
            raise ValueError("A least one mine is required")

    def __contains__(self, xy):
        return 0 <= xy[0] < self.width and 0 <= xy[1] < self.height

    def __repr__(self):
        return f'{type(self).__name__}({self.width}, {self.height}, {len(self.mines)})'

    def __str__(self):
        meta_text = (
            f'**Marked:** {self.mines_marked} / {self.mine_count}\n'
            f'**Flags Remaining:** {self.remaining_flags}'
        )

        top_row = ' '.join(REGIONAL_INDICATORS[:self.width])
        string = '\n'.join([
            f"{char} {' '.join(map(str, cells))}"
            for char, cells in zip(REGIONAL_INDICATORS, self._board)
        ])

        return f'{meta_text}\n\u200b\n\N{BLACK LARGE SQUARE} {top_row}\n{string}'

    def _place_mines_from(self, x, y):
        surrounding = set(self._get_neighbours(x, y))
        click_area = surrounding | {(x, y)}

        possible_coords = itertools.product(range(self.width), range(self.height))
        coords = [p for p in possible_coords if p not in click_area]

        self.mines = set(random.sample(coords, k=min(self._mine_count, len(coords))))
        self.mines.update(random.sample(surrounding, self._mine_count - len(self.mines)))

        # All mines should be exhausted, unless we somehow made a malformed board.
        assert len(self.mines) == self._mine_count, f"only {len(self.mines)} mines were placed"

    def is_mine(self, x, y):
        return (x, y) in self.mines

    def is_flag(self, x, y):
        return (x, y) in self.flags

    def is_visible(self, x, y):
        return (x, y) in self.visible

    def is_unsure(self, x, y):
        return (x, y) in self.unsures

    def _get_neighbours(self, x, y):
        pairs = ((x + surr_x, y + surr_y) for (surr_x, surr_y) in SURROUNDING)
        return (p for p in pairs if p in self)

    def show(self, x, y):
        if not self.mines:
            self._place_mines_from(x, y)

        if self.is_visible(x, y):
            return

        self.visible.add((x, y))
        if self.is_mine(x, y) and not self.is_flag(x, y):
            raise HitMine(x, y)

        surrounding = sum(self.is_mine(nx, ny) for nx, ny in self._get_neighbours(x, y))
        if not surrounding:
            self._board[y][x] = Tile.shown
            for nx, ny in self._get_neighbours(x, y):
                self.show(nx, ny)
        else:
            self._board[y][x] = Tile.numbered(surrounding)

    def _modify_board(self, x, y, attr):
        if self.is_visible(x, y):
            return

        tup = x, y
        was_thing = getattr(self, f'is_{attr}')(x, y)
        for thing in ('flags', 'unsures'):
            getattr(self, thing).discard(tup)

        if was_thing:
            self._board[y][x] = Tile.blank
        else:
            getattr(self, f'{attr}s').add(tup)
            self._board[y][x] = getattr(Tile, attr)

    def flag(self, x, y):
        self._modify_board(x, y, 'flag')

    def unsure(self, x, y):
        self._modify_board(x, y, 'unsure')

    def reveal_mines(self, success=False):
        tile = Tile.flag if success else Tile.boom
        for mx, my in self.mines:
            self._board[my][mx] = tile

    def hide_mines(self):
        for mx, my in self.mines:
            self._board[my][mx] = Tile.blank

    def explode(self, x, y):
        if not self.is_visible(x, y):
            return
        self._board[y][x] = Tile.boom

    def is_solved(self):
        return len(self.visible) + len(self.mines) == self.width * self.height

    @property
    def width(self):
        return len(self._board[0])

    @property
    def height(self):
        return len(self._board)

    @property
    def mine_count(self):
        return len(self.mines) or self._mine_count

    @property
    def mines_marked(self):
        return len(self.flags)

    @property
    def remaining_flags(self):
        return self.mine_count - self.mines_marked

    @property
    def remaining_mines(self):
        return len(self.mines - self.flags)

    @classmethod
    def beginner(cls):
        """Returns a beginner minesweeper board"""
        return cls(9, 9, 10)

    @classmethod
    def intermediate(cls):
        """Returns a intermediate minesweeper board"""
        return cls(12, 12, 20)

    @classmethod
    def expert(cls):
        """Returns an expert minesweeper board"""
        return cls(13, 13, 40)


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
                .set_author(name='Minesweeper Help')
                .add_field(name=instructions, value=self.reaction_help)
                )

    @page('\N{VIDEO GAME}')
    def controls(self):
        """Controls"""
        text = textwrap.dedent(f'''
        **Type in this format:**
        ```
        column row
        ```
        Use `A-{ascii_uppercase[self._game._board.width - 1]}` for the column
        and `A-{ascii_uppercase[self._game._board.height - 1]}` for the row.
        \u200b
        To flag a tile, type `f` or `flag` after the row.
        If you're unsure about a tile, type `u` or `unsure` after the row.
        ''')
        return (discord.Embed(colour=self.colour, description=text)
                .set_author(name='Instructions')
                .add_field(name='In-game Reactions', value=self._game._controller.reaction_help)
                )

    @staticmethod
    def _possible_spaces():
        number = random.randint(1, 9)
        return textwrap.dedent(f'''
        {Tile.shown} = Empty tile, reveals numbers or other empties around it.
        {Tile.numbered(number)} = Number of mines around it. This one has {pluralize(mine=number)}.
        {Tile.boom} = BOOM! Hitting a mine will instantly end the game.
        {Tile.flag} = A flagged tile means it *might* be a mine.
        {Tile.unsure} = It's either a mine or not. No one's sure.
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
                .set_author(name='Tiles')
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
            coro = _MinesweeperHelp(self.context, self._game).interact(timeout=300)
            self._help_future = asyncio.ensure_future(coro)

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

class MinesweeperSession:
    def __init__(self, ctx, level, board):
        self._board = board
        self._ctx = ctx
        self._header = f'Minesweeper - {level}'
        self._controller = _Controller(ctx, self)

    @property
    def display(self):
        board, ctx = self._board, self._ctx
        description = f'**Player:** {ctx.author}\n{board}'

        return (discord.Embed(colour=ctx.bot.colour, description=description)
                .set_author(name=self._header, icon_url=ctx.author.avatar_url)
                .add_field(name='Stuck?', value='For help, click \N{INFORMATION SOURCE}.')
                )

    def _check_message(self, message):
        return (self._controller.can_poll()
                and message.channel == self._ctx.channel
                and message.author == self._ctx.author)

    def _parse_message(self, content):
        splitted = content.lower().split(None, 3)[:3]
        chars = len(splitted)

        if chars == 2:
            flag = FlagType.default
        elif chars == 3:
            flag = getattr(FlagType, splitted[2].lower(), FlagType.default)
        else:  # We need at least the x, y coordinates...
            return None

        try:
            x, y = map(ascii_lowercase.index, splitted[:2])
        except ValueError:
            return None
        else:
            if (x, y) not in self._board:
                return None
            return x, y, flag

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
                message = await wait_for('message', timeout=120, check=self._check_message)
            except asyncio.TimeoutError:
                if self._controller.can_poll():
                    raise
                continue

            parsed = self._parse_message(message.content)
            if parsed is None:      # garbage input, ignore.
                continue

            x, y, thing = parsed
            with contextlib.suppress(discord.HTTPException):
                await message.delete()

            getattr(self._board, thing.value)(x, y)

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
            self._controller.interact(timeout=None, delete_after=False)
        ]

        # TODO: Timing context manager?
        start = time.perf_counter()
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        end = time.perf_counter()

        for p in pending:
            p.cancel()

        edit = self.edit
        delete_edit = partial(edit, delete_after=7)

        # This can probably be moved away and cleaned up somehow but whatever
        try:
            await f
        except asyncio.CancelledError:
            # The future would be cancelled above as any pending futures would
            # be cancelled.
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
            self._board.reveal_mines()
            await delete_edit(0xFF0000, 'Game Over!', icon=GAME_OVER_ICON)
            return False, -1
        else:
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
                     .set_author(name='Fastest times')
                     )

            query = """SELECT user_id, time FROM minesweeper_games
                       WHERE won AND level = $1
                       ORDER BY time
                       LIMIT 10;
                    """
            records = await self.context.db.fetch(query, difficulty.value)
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
        channel_id = ctx.cog.minesweeper_sessions.get(ctx.author.id)
        if channel_id:
            raise AlreadyPlaying(f'Please finish your game in <#{channel_id}> first.')
        return True
    return commands.check(predicate)


class Minesweeper(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self.minesweeper_sessions = {}

    async def __error(self, ctx, error):
        if isinstance(error, AlreadyPlaying):
            await ctx.send(error)

    @contextlib.contextmanager
    def _create_session(self, ctx):
        self.minesweeper_sessions[ctx.author.id] = ctx.channel.id
        try:
            yield
        finally:
            self.minesweeper_sessions.pop(ctx.author.id, None)

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
    async def minesweeper(self, ctx, level: Level = Level.beginner):
        """Starts a game of Minesweeper"""
        with self._create_session(ctx):
            if level is Level.custom:
                ctx.command = self.minesweeper_custom
                return await ctx.reinvoke()

            board = getattr(Board, level.name)()
            await self._do_minesweeper(ctx, level, board)

    @minesweeper.command(name='custom')
    @not_playing_minesweeper()
    async def minesweeper_custom(self, ctx, width: int, height: int, mines: int):
        """Starts a custom game of Minesweeper"""
        with self._create_session(ctx):
            try:
                board = Board(width, height, mines)
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
