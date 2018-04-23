import asyncio
import contextlib
import enum
import itertools
import random
import re
import textwrap

import discord
from discord.ext import commands
from more_itertools import flatten, grouper, sliced

from .manager import SessionManager
from ..utils.paginator import BaseReactionPaginator, page
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

    def validate(self):
        # If the board is not full then it's not valid.
        if EMPTY in flatten(self._board):
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


class LockedMessage:
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


# Controller States I guess...
IN_GAME = 0
ON_HELP = 1
ON_SAVE = 2


class Controller(BaseReactionPaginator):
    def __init__(self, ctx, game):
        super().__init__(ctx)
        self._game = game
        self._future = ctx.bot.loop.create_future()
        self._future.set_result(None)  # We just need an already done future.
        self._state = IN_GAME

    @property
    def display(self):
        return self._game._display

    def default(self):
        return self.display

    def in_game(self):
        return self._state == IN_GAME

    def edit_message(self, colour, header):
        if not self._future.done():
            self._future.cancel()

        board = self._game._board
        d = self.display
        d.description = f'**{header}**\n{board}'
        d.colour = colour

        async def wait():
            await asyncio.sleep(3)
            d.colour = self.context.bot.colour
            d.description = str(board)
            await self._message.edit(embed=self.display)

        self._future = asyncio.ensure_future(wait())

    @page('\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}')
    def restart(self):
        """Restart"""
        if not self.in_game():
            return None

        board = self._game._board
        board.clear()
        self.display.description = str(board)
        return self.display

    @page('\N{WHITE HEAVY CHECK MARK}')
    def validate(self):
        """Check"""
        if not self.in_game():
            return None

        board = self._game._board
        try:
            board.validate()
        except ValueError as e:
            self.edit_message(0xF44336, f'{e}\n')
        else:
            d = self.display
            d.description = f'**Sudoku Complete!**\n\n{self._game._board}'
            d.colour = 0x4CAF50

            # stop() is a coro and it prompts if the user wants to save
            # so we can't use that here.
            self._paginating = False
            if not self._future.done():
                self._future.cancel()

        return self.display

    @page('\N{INFORMATION SOURCE}')
    def info(self):
        """Help"""
        self._state = ON_HELP

        help_text = textwrap.dedent('''
            The goal is to fill each space with a number
            from 1 to 9, such that each row, column, and
            3 x 3 box contains each number exactly **once**.
        ''')

        input_field = textwrap.dedent('''
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
            To check your board, click \N{WHITE HEAVY CHECK MARK}.
            You must fill the whole board first.
            \u200b
            If the board is correctly filled,
            you've completed the game!
            \u200b
        ''')

        return (discord.Embed(colour=self.context.bot.colour, description=help_text)
                .set_author(name='Sudoku Help!')
                .add_field(name='How to play', value=input_field)
                .add_field(name='Buttons', value=self.reaction_help, inline=False)
                )

    @page('\N{INPUT SYMBOL FOR NUMBERS}')
    def resume(self):
        """Resume game"""
        if self.in_game():
            return None

        self._state = IN_GAME
        return self.display

    async def _confirm(self, prompt, *, timeout=None):
        ctx = self.context
        choices = {'y', 'yes', 'n', 'no'}

        def check(m):
            return (m.channel == self._message.channel
                    and m.author == ctx.author
                    and m.content.lower() in choices
                    )

        d = self.display
        d.description = f'**{prompt}**\n(Type `yes` or `no`)\n\u200b\n{self._game._board}'

        d.colour = 0xF44336
        await self._message.edit(embed=d)

        try:
            message = await ctx.bot.wait_for('message', check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None

        # XXX: This is LBYL to avoid excessive requests. Should I use EAFP anyway?
        if ctx.me.permissions_in(ctx.channel).manage_messages:
            await message.delete()

        return message.content.lower() in {'yes', 'y'}

    async def _confirm_save(self):
        self._state = ON_SAVE
        board = self._game._board

        if not board.new:
            return True

        ctx = self.context

        query = 'SELECT 1 FROM saved_sudoku_games WHERE user_id = $1;'
        await ctx.acquire()
        row = await ctx.db.fetchrow(query, ctx.author.id)
        await ctx.release()

        if not row:
            return True

        return await self._confirm('A save game already exists. Overwrite it?', timeout=25)

    @page('\N{FLOPPY DISK}')
    async def save(self):
        """Save game"""
        if not self.in_game():
            return None

        if not self._game._board.dirty:
            # We don't need to save a game if we didn't make any changes.
            # So let's save us some requests and DB acquiring.
            return None

        try:
            if not await self._confirm_save():
                d = self.display
                d.description = str(self._game._board)
                d.colour = self.colour
                return d

            await self._game.save()
            self.edit_message(0x3F51B5, 'Game saved!\n')
            return self.display
        finally:
            self._state = IN_GAME

    @page('\N{BLACK SQUARE FOR STOP}')
    async def stop(self):
        """Quit"""
        super().stop()

        if not self._future.done():
            self._future.cancel()

        # Description will be edited when we do the two prompts.
        old_description = self._current.description

        if self._game._board.dirty:
            # We don't want them to hang the game.
            self._state = ON_SAVE
            save_changes = await self._confirm("There are unsaved changes. Save game?", timeout=25)
            if save_changes and await self._confirm_save():
                await self._game.save()

        self._current.colour = 0x607D8B
        self._current.description = old_description
        return self._current


def _legacy_parse(string):
    x, y, number, = string.lower().split()

    number = int(number)
    if not 1 <= number <= 9:
        raise ValueError("number must be between 1 and 9")

    return _letters.index(x), _letters.index(y), number

_INPUT_REGEX = re.compile('([a-i])\s{0,1}([1-9])\s{0,1}([0-9]|clear)')

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


def _parse_input(string):
    # Really not sure if I should put this in the board object...
    for parser in [_parse, _legacy_parse]:
        try:
            return parser(string)
        except ValueError:
            continue
    return None


class SudokuSession:
    __slots__ = ('_board', '_controller', '_ctx', '_display')

    def __init__(self, ctx, board):
        self._board = board
        self._ctx = ctx
        if ctx.bot_has_permissions(external_emojis=True):
            self._board._clue_markers = ctx.bot.emoji_config.sudoku_clues

        self._controller = Controller(ctx, self)

        a = ctx.author
        self._display = (discord.Embed(colour=ctx.bot.colour, description=str(self._board))
                         .set_author(name=f'Sudoku: {a.display_name}', icon_url=a.avatar_url)
                         .add_field(name='\u200b', value='Stuck? Click \N{INFORMATION SOURCE} for help', inline=False)
                         )

    def check(self, message):
        if not (self._controller.in_game()
                and message.channel == self._ctx.channel
                and message.author == self._ctx.author):
            return

        # Parse the input right away so that we don't have any
        # random messages resetting the timer.
        result = _parse_input(message.content)
        if result is None:
            return
        x, y, number = result

        try:
            self._board[x, y] = number
        except (IndexError, ValueError):
            return

        return True

    async def _loop(self):
        # TODO: Set an event and add a wait_until_ready method on the paginator
        while not self._controller._message:
            await asyncio.sleep(0)

        # Wrap message in a lock so we don't have the messages and the reactions
        # making it go all wonky.
        self._controller._message = LockedMessage(self._controller._message)
        wait_for = self._ctx.bot.wait_for

        timeout = 300 * (self._board.difficulty + 1) / 2

        while True:
            try:
                message = await wait_for('message', timeout=timeout, check=self.check)
            except asyncio.TimeoutError:
                if not self._controller.in_game():
                    continue

                await self._ctx.send(f'{self._ctx.author.mention} You took too long!')
                break

            with contextlib.suppress(discord.HTTPException):
                await message.delete()

            self._display.description = str(self._board)
            await self._controller._message.edit(embed=self._display)

    async def save(self):
        ctx = self._ctx
        args = self._board.to_data()

        query = """INSERT INTO saved_sudoku_games (user_id, board, clues)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (user_id)
                   DO UPDATE SET board=$2, clues=$3;
                """

        await ctx.acquire()
        await ctx.db.execute(query, ctx.author.id, *args)
        await ctx.release()

        self._board.new = False
        self._board.dirty = False  # We saved the game, no new changes to save

    async def run(self):
        coros = [
           self._loop(),
           self._controller.interact(timeout=None, delete_after=False, release_connection=False),
        ]

        await self._ctx.release()
        done, pending = await asyncio.wait(coros, return_when=asyncio.FIRST_COMPLETED)

        for p in pending:
            p.cancel()

        try:
            await done.pop()
        except commands.BotMissingPermissions as e:
            await self._ctx.bot_missing_perms(e.missing_perms)
            return

        # The message has to be deleted in order to mitigate lag from the emojis
        # in the embed.
        async def task():
            await asyncio.sleep(5)
            with contextlib.suppress(discord.HTTPException):
                await self._controller._message.delete()

        self._ctx.bot.loop.create_task(task())


class Sudoku:
    def __init__(self, bot):
        self.bot = bot
        self.sudoku_sessions = SessionManager()

    async def _get_difficulty(self, ctx):
        # Get the saved board if it exists
        reactions = ['1\u20e3', '2\u20e3', '3\u20e3', '4\u20e3']
        difficulties = ['easy', 'medium', 'hard', 'extreme']

        # Check if the user has a game saved already
        query = 'SELECT * FROM saved_sudoku_games WHERE user_id = $1;'
        board = await ctx.db.fetchrow(query, ctx.author.id)
        if board is not None:
            reactions.append('\U0001f4be')
            difficulties.append('resume game')
            board = Board.from_data(board)

        is_valid = frozenset(reactions).__contains__

        prompt = discord.Embed(colour=ctx.bot.colour)
        prompt.set_author(name=f"Let's play Sudoku, {ctx.author.display_name}!", icon_url=SUDOKU_ICON)
        prompt.description = '**Please choose a difficulty.**\n-----------------------\n'
        prompt.description += '\n'.join(
            f'{reaction} = {diff.title()}'
            for reaction, diff in zip(reactions, difficulties)
        )

        message = await ctx.send(embed=prompt)

        # TODO: I use this function a lot. Maybe I should make a
        #       helper function...
        async def put(message, emojis):
            for e in emojis:
                await message.add_reaction(e)

        future = asyncio.ensure_future(put(message, reactions))

        def check(reaction, user):
            return (reaction.message.id == message.id
                    and user == ctx.author
                    and is_valid(reaction.emoji)
                    )

        try:
            reaction, _ = await ctx.bot.wait_for('reaction_add', check=check, timeout=20)
        except asyncio.TimeoutError:
            await ctx.send('You took too long...')
            return None
        finally:
            await message.delete()
            if not future.done():
                future.cancel()

        if reaction.emoji[-1] != '\u20e3':
            return board

        index = int(reaction.emoji[0]) - 1
        return getattr(Board, difficulties[index])()

    @commands.command()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
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
