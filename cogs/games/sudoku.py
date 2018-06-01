import asyncio
import enum
import functools
import itertools
import random
import re
import textwrap

import discord
from discord.ext import commands
from more_itertools import flatten, grouper, sliced

from .manager import SessionManager
from ..utils.paginator import InteractiveSession, trigger
from ..utils.misc import emoji_url

__schema__ = """
    CREATE TABLE IF NOT EXISTS saved_sudoku_games (
        user_id BIGINT PRIMARY KEY,
        board SMALLINT[9][9] NOT NULL,  -- size is not enforced but w/e
        clues SMALLINT[] NOT NULL
    );
"""

SUDOKU_ICON = emoji_url('\N{INPUT SYMBOL FOR NUMBERS}')
# Default Sudoku constants
BLOCK_SIZE = 3
BOARD_SIZE = 81
EMPTY = 0


# Sudoku board generator by Gareth Rees
# This works best when m = 3.
# For some reason it goes significantly slower when m >= 4
# And it doesn't work when m = 2
def _make_board(m=3):
    """Return a random filled m**2 x m**2 Sudoku board."""
    n = m * m
    nn = n * n
    board = [[None] * n for _ in range(n)]

    def search(c=0):
        i, j = divmod(c, n)
        i0, j0 = i - i % 3, j - j % 3  # Origin of mxm block
        numbers = random.sample(range(1, n + 1), n)
        for x in numbers:
            if (x not in board[i]                      # row
                and all(row[j] != x for row in board)  # column
                and all(x not in row[j0:j0+m]          # block
                        for row in board[i0:i])):
                board[i][j] = x
                if c + 1 >= nn or search(c + 1):
                    return board
        else:
            # No number is valid in this cell: backtrack and try again.
            board[i][j] = None
            return None

    return search()


def _get_squares(board):
    size = len(board[0])
    subsize = int(size ** 0.5)
    for slice_ in sliced(board, subsize):
        yield from zip(*(sliced(line, subsize) for line in slice_))


def _get_coords(size):
    return itertools.product(range(size), repeat=2)


_letter_markers = [chr(i) for i in range(0x1f1e6, 0x1f1ef)]
_number_markers = [f'{i}\u20e3' for i in range(1, 10)]
_top_row = '  '.join(map('\u200b'.join, grouper(3, _letter_markers)))
_top_row = '\N{SOUTH EAST ARROW}  ' + _top_row
_letters = 'abcdefghi'


DEFAULT_CLUE_EMOJIS = tuple(_number_markers)


class Board:
    __slots__ = ('_board', '_clues', '_clue_markers', 'new', 'dirty')

    def __init__(self, clues):
        self.new = True
        self.dirty = True  # So we can save this right away
        self._board = _make_board()

        # put holes in the board.
        coords = list(_get_coords(BLOCK_SIZE * BLOCK_SIZE))
        random.shuffle(coords)
        it = iter(coords)

        # slice the iterator first to get the "clues"
        self._clues = set(itertools.islice(it, clues))

        # Fill the rest
        for p in it:
            self[p] = EMPTY

        # Needed to specially mark the clues.
        self._clue_markers = DEFAULT_CLUE_EMOJIS

    def __getitem__(self, xy):
        x, y = xy
        return self._board[y][x]

    def __setitem__(self, xy, value):
        if xy in self._clues:
            raise ValueError("cannot place a number in a pre-placed clue")

        x, y = xy
        self._board[y][x] = value
        self.dirty = True

    def __repr__(self):
        return f'{self.__class__.__name__}(clues={len(self._clues)!r})'

    def __str__(self):
        fmt = "{0}  {1}{2}{3}  {4}{5}{6}  {7}{8}{9}"
        clues = self._clues
        clue_markers = self._clue_markers

        def draw_cell(y, cell_pair):
            x, cell = cell_pair
            if not cell:
                return '\N{BLACK LARGE SQUARE}'

            return clue_markers[cell - 1] if (x, y) in clues else f'{cell}\u20e3'

        return _top_row + '\n' + '\n'.join(
            fmt.format(
                _number_markers[i],
                *map(draw_cell, itertools.repeat(i), enumerate(line)),
                '\N{WHITE SMALL SQUARE}'
            )
            + '\n' * (((i + 1) % 3 == 0))
            for i, line in enumerate(self._board)
        )

    def is_full(self):
        return EMPTY not in flatten(self._board)

    def validate(self):
        # If the board is not full then it's not valid.
        if not self.is_full():
            raise ValueError('Fill the board first.')

        row_markers = range(1, len(self._board[0]) + 1)
        column_markers = _letters.upper()

        required_nums = set(row_markers)

        def check(lines, header, seq):
            lines = enumerate(map(set, lines))
            if all(line == required_nums for _, line in lines):
                return

            # If this is exhausted then the all clause would've been True
            # and thus this won't be executed.
            num, _ = next(lines, (10, None))
            raise ValueError(f'{header} {seq[num - 1]} is invalid')

        # Check rows
        check(self._board, 'Row', row_markers)
        # Check columns
        check(zip(*self._board), 'Column', column_markers)
        # Check boxes
        check(map(flatten, _get_squares(self._board)), 'Box', row_markers)

    def clear(self):
        non_clues = itertools.filterfalse(self._clues.__contains__, _get_coords(len(self._board)))
        for p in non_clues:
            self[p] = EMPTY

    def to_data(self):
        size = len(self._board[0])
        return self._board, [x * size + y for x, y in self._clues]

    @classmethod
    def from_data(cls, data):
        # We are bypassing __init__ here since it doesn't apply here.
        size = BLOCK_SIZE * BLOCK_SIZE
        self = cls.__new__(cls)

        self._board = data['board']
        self._clues = {divmod(clue, size) for clue in data['clues']}
        self.new = False
        self.dirty = False  # We don't need to save a game we just loaded.
        self._clue_markers = DEFAULT_CLUE_EMOJIS  # Needed to specially mark the clues.

        return self

    @classmethod
    def beginner(cls):
        """Returns a sudoku board suitable for beginners"""
        return cls(clues=random.randint(40, 45))

    @classmethod
    def intermediate(cls):
        """Returns a sudoku board suitable for intermediate players"""
        return cls(clues=random.randint(27, 36))

    @classmethod
    def expert(cls):
        """Returns a sudoku board suitable for experts"""
        return cls(clues=random.randint(19, 22))

    @classmethod
    def minimum(cls):
        """Returns a sudoku board with the minimum amount of clues needed
        to achieve a unique solution.
        """
        return cls(clues=17)

    # difficulty aliases
    easy = beginner
    medium = intermediate
    hard = expert
    extreme = minimum

    @property
    def difficulty(self):
        num_clues = len(self._clues)
        ranges = [(40, 45), (27, 36), (19, 22), (0, 17)]

        for i, (low, high) in enumerate(ranges, 1):
            if low <= num_clues <= high:
                return i

        return -1


class _EnumConverter:
    def __str__(self):
        return self.name.title()

    @classmethod
    async def convert(cls, ctx, arg):
        lowered = arg.lower()
        try:
            return cls[lowered].name
        except KeyError:
            difficulties = '\n'.join(str(m).lower() for m in cls)
            raise commands.BadArgument(
                f'"{arg}"" is not a difficulty. Valid difficulties:\n{difficulties}'
            ) from None

    @classmethod
    def random_example(cls, ctx):
        return random.choice(list(cls._member_map_))

_difficulties = [n for n, v in Board.__dict__.items() if isinstance(v, classmethod)]
_difficulties.remove('from_data')
Difficulty = enum.Enum('Difficulty', _difficulties, type=_EnumConverter)


HELP_TEXT = '''
The goal is to fill each space with a number
from 1 to 9, such that each row, column, and
3 x 3 box contains each number exactly **once**.
'''

INPUT_FIELD = '''
Send a message in the following format:
`letter number number`
\u200b
Use `A-I` for `row` and `column`.
Use `1-9` for the number.
Use `0` or `clear` for the second number
if you want to clear the tile.
-------------------------
Examples: **`a 1 2`**, **`B65`**, **`D 7 clear`**
\u200b
If the board is correctly filled,
you've completed the game!
\u200b
'''


class SudokuHelp(InteractiveSession, stop_fallback='exit help'):
    def __init__(self, ctx, game):
        super().__init__(ctx)
        self._game = game

    def default(self):
        # TODO: Paginate?
        return (discord.Embed(colour=self._bot.colour, description=HELP_TEXT)
                .set_author(name='Sudoku Help!')
                .add_field(name='How to play', value=INPUT_FIELD)
                .add_field(name='Controls', value=self.help(), inline=False)
                )

    def help(self):
        if self._game.using_reactions():
            return self._game.reaction_help

        return '\n'.join(
            f'`{name}` = {func.__doc__}'
            for name, func in self._game._message_fallbacks
        )


_INPUT_REGEX = re.compile(r'([a-i])\s?([1-9])\s?([0-9]|clear)')

class SudokuSession(InteractiveSession):
    def __init__(self, ctx, board):
        super().__init__(ctx)
        self._board = board

        if ctx.bot_has_permissions(external_emojis=True):
            self._board._clue_markers = ctx.bot.emoji_config.sudoku_clues

        self._future = ctx.bot.loop.create_future()
        self._future.set_result(None)

        self._help_future = ctx.bot.loop.create_future()
        self._help_future.set_result(None)

        self._help = SudokuHelp(ctx, self).run

    def default(self):
        a = self.context.author
        if self.using_reactions():
            help_text = 'Stuck? Click \N{INFORMATION SOURCE} for help'
        else:
            help_text = 'Stuck? Type `help` for help'

        return (discord.Embed(colour=self._bot.colour, description=str(self._board))
                .set_author(name=f'Sudoku: {a.display_name}', icon_url=a.avatar_url)
                .add_field(name='\u200b', value=help_text, inline=False)
                )

    # ----------- Triggers ----------

    async def _queue_edit(self, colour, header):
        if not self._future.done():
            self._future.cancel()

        d = self.default()
        d.description = f'**{header}**\n{d.description}'
        d.colour = colour
        await self._message.edit(embed=d)

        async def wait():
            await asyncio.sleep(3)
            await self._message.edit(embed=self.default())

        self._future = asyncio.ensure_future(wait())

    @trigger('\N{INFORMATION SOURCE}', fallback='help')
    def info(self):
        """Help"""
        if not self._help_future.done():
            return

        self._help_future = self._bot.loop.create_task(self._help(timeout=None))

    @trigger('\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}', fallback='restart')
    def restart(self):
        """Restart"""
        if not self._help_future.done():
            self._help_future.cancel()

        self._board.clear()
        return self.default()

    # ---------- Save Game ---------------

    async def _confirm(self, prompt, *, timeout=None):
        ctx = self.context
        choices = {'y', 'yes', 'n', 'no'}

        def check(m):
            return (m.channel == self._channel
                    and m.author == ctx.author
                    and m.content.lower() in choices
                    )

        d = self.default()
        d.description = f'**{prompt}**\n(Type `yes` or `no`)\n\u200b\n{self._board}'
        d.colour = 0xF44336

        await self._message.edit(embed=d)

        try:
            message = await ctx.bot.wait_for('message', check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return False

        # XXX: This is LBYL to avoid excessive requests. Should I use EAFP anyway?
        if ctx.me.permissions_in(ctx.channel).manage_messages:
            await message.delete()

        return message.content.lower() in {'yes', 'y'}

    async def _confirm_save(self):
        board = self._board

        if not board.new:
            return True

        ctx = self.context

        query = 'SELECT 1 FROM saved_sudoku_games WHERE user_id = $1;'
        row = await self._bot.pool.fetchrow(query, ctx.author.id)

        if not row:
            return True

        return await self._confirm('A save game already exists. Overwrite it?', timeout=25)

    async def _save(self):
        ctx = self.context
        args = self._board.to_data()

        query = """INSERT INTO saved_sudoku_games (user_id, board, clues)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (user_id)
                   DO UPDATE SET board=$2, clues=$3;
                """

        await self._bot.pool.execute(query, ctx.author.id, *args)
        self._board.new = False
        self._board.dirty = False  # We saved the game, no new changes to save

    @trigger('\N{FLOPPY DISK}', fallback='save', block=True)
    async def save(self):
        """Save game"""
        if not self._board.dirty:
            # We don't need to save a game if we didn't make any changes.
            # So let's save us some requests and DB acquiring.
            return None

        if not self._help_future.done():
            self._help_future.cancel()

        if not await self._confirm_save():
            return self.default()

        await self._save()
        await self._queue_edit(0x3F51B5, 'Game saved!\n')

    # ---------- Stop ----------

    @trigger('\N{BLACK SQUARE FOR STOP}', fallback='exit', block=True)
    async def stop(self):
        """Quit"""
        await super().stop()

        if not self._future.done():
            self._future.cancel()

        if not self._help_future.done():
            self._help_future.cancel()

        # Description will be edited when we do the two prompts.
        old_description = self._current.description

        if self._board.dirty:
            save_changes = await self._confirm("There are unsaved changes. Save game?", timeout=25)
            if save_changes and await self._confirm_save():
                await self._save()

        d = self.default()
        d.colour = 0x607D8B
        return d

    # ---------- Message Parsing ----------

    @staticmethod
    def _legacy_parse(string):
        x, y, number, = string.lower().split()

        number = int(number)
        if not 1 <= number <= 9:
            raise ValueError("number must be between 1 and 9")

        return _letters.index(x), _letters.index(y), number

    @staticmethod
    def _parse(string):
        match = _INPUT_REGEX.match(string.lower())
        if match is None:
            raise ValueError('invalid input format')

        x, y, number = match.groups()

        if number in ['clear', '0']:
            number = EMPTY
        else:
            number = int(number)

        return _letters.index(x), int(y) - 1, number

    def _parse_input(self, string):
        # Really not sure if I should put this in the board object...
        for parse in [self._parse, self._legacy_parse]:
            try:
                return parse(string)
            except ValueError:
                continue
        return None

    async def __validate(self):
        embed = self.default()
        if not self._board.is_full():
            return embed

        try:
            self._board.validate()
        except ValueError as e:
            await self._queue_edit(0xF44336, f'{e}\n')
            return
        else:
            embed.description = f'**Sudoku Complete!**\n\n{self._board}'
            embed.colour = 0x4CAF50

            # stop() is a coro and it prompts if the user wants to save
            # so we can't use that here.
            await self._queue.put(None)
            if not self._future.done():
                self._future.cancel()

            return embed

    async def _edit_board(self, x, y, number, *_):
        if not self._help_future.done():
            self._help_future.cancel()

        try:
            self._board[x, y] = number
        except (IndexError, ValueError):
            return

        return await self.__validate()

    async def on_message(self, message):
        if (
            self._blocking
            or message.channel != self._channel
            or message.author.id not in self._users
        ):
            return

        # Parse the input right away so that we don't have any
        # random messages resetting the timer.
        result = self._parse_input(message.content)
        if result is None:
            return

        await self._queue.put((functools.partial(self._edit_board, *result), message.delete))

    async def cleanup(self, **kwargs):
        await asyncio.sleep(5)
        await super().cleanup(**kwargs)

    async def run(self):
        with self._bot.temp_listener(self.on_message):
            try:
                timeout = 300 * (self._board.difficulty + 1) / 2
                await super().run(timeout=timeout)
            except commands.BotMissingPermissions as e:
                await self.context.bot_missing_perms(e.missing_perms)


def _board_setter(emoji, name, method):
    @trigger(emoji, fallback=f'{emoji[0]}|{name.lower()}')
    async def set_func(self):
        self.board = method()
        await self.stop()
    set_func.__name__ = name
    return set_func

class SudokuMenu(InteractiveSession, stop_emoji=None, stop_fallback=None):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._saved_board = None
        self.board = None
        self._reaction_map = SudokuMenu._reaction_map.copy()

    easy    = _board_setter('1\u20e3', 'Easy',    Board.easy)
    medium  = _board_setter('2\u20e3', 'Medium',  Board.medium)
    hard    = _board_setter('3\u20e3', 'Hard',    Board.hard)
    extreme = _board_setter('4\u20e3', 'Extreme', Board.extreme)

    @trigger('\U0001f4be', fallback='resume|load')
    async def resume_game(self):
        self.board = self._saved_board
        await self.stop()

    def default(self):
        prompt = discord.Embed(colour=self._bot.colour)
        prompt.set_author(name=f"Let's play Sudoku, {self.context.author.display_name}!", icon_url=SUDOKU_ICON)
        prompt.description = '**Please choose a difficulty.**\n-----------------------\n'
        prompt.description += '\n'.join(
            f'{trig} = {callback.func.__name__.title().replace("_", " ")}'
            for trig, callback in self._reaction_map.items()
        )
        return prompt

    async def start(self):
        query = 'SELECT * FROM saved_sudoku_games WHERE user_id = $1;'
        board = await self._bot.pool.fetchrow(query, self.context.author.id)

        if board is None:
            del self._reaction_map['\U0001f4be']
        else:
            self._saved_board = Board.from_data(board)

        await super().start()


class Sudoku:
    def __init__(self, bot):
        self.bot = bot
        self.sudoku_sessions = SessionManager()

    async def _get_difficulty(self, ctx):
        menu = SudokuMenu(ctx)
        try:
            await asyncio.wait_for(menu.run(), timeout=20)
        except asyncio.TimeoutError:
            await ctx.send('Took too long...')
            return None
        return menu.board

    @commands.command()
    @commands.bot_has_permissions(embed_links=True)
    async def sudoku(self, ctx, difficulty: Difficulty=None):
        if self.sudoku_sessions.session_exists(ctx.author.id):
            return await ctx.send('Please finish your other Sudoku game first.')

        if difficulty is None:
            board = await self._get_difficulty(ctx)
        else:
            board = getattr(Board, difficulty)()

        if board is None:
            return

        with self.sudoku_sessions.temp_session(ctx.author.id, SudokuSession(ctx, board)) as inst:
            await inst.run()


def setup(bot):
    bot.add_cog(Sudoku(bot))
