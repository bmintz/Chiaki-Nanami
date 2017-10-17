import asyncio
import discord
import enum
import itertools
import random
import textwrap

from contextlib import suppress
from discord.ext import commands
from more_itertools import grouper, interleave, iter_except

from .manager import SessionManager

from ..utils.paginator import BaseReactionPaginator, page

from core.cog import Cog

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
        i0, j0 = i - i % 3, j - j % 3 # Origin of mxm block
        numbers = list(range(1, n + 1))
        random.shuffle(numbers)
        for x in numbers:
            if (x not in board[i]                     # row
                and all(row[j] != x for row in board) # column
                and all(x not in row[j0:j0+m]         # block
                        for row in board[i0:i])):
                board[i][j] = x
                if c + 1 >= nn or search(c + 1):
                    return board
        else:
            # No number is valid in this cell: backtrack and try again.
            board[i][j] = None
            return None

    return search()

# Default Sudoku constants
BLOCK_SIZE = 3
BOARD_SIZE = 81

class Board:
    def __init__(self, holes):
        self._solved = _make_board()
        ss = BLOCK_SIZE * BLOCK_SIZE

        # put holes in the board.
        self._board = [row[:] for row in self._solved]
        coords = list(itertools.product(range(ss), range(ss)))
        random.shuffle(coords)
        it = iter(coords)
        for x, y in itertools.islice(it, holes):
            self._board[y][x] = None

        self._pre_placed_numbers = set(it)
        self._placed_numbers = set()
        # for cells where the solver puts multiple numbers in.
        self.stored_numbers = {}

    def __getitem__(self, xy):
        x, y = xy
        return self._board[y][x]

    def __setitem__(self, xy, value):
        if xy in self._pre_placed_numbers:
            raise ValueError("cannot place a number there")

        x, y = xy
        self._board[y][x] = value

        if value != 0:
            self.stored_numbers.pop(xy, None)
        self._placed_numbers.add(xy)

    def __str__(self):
        spacer = "++---+---+---++---+---+---++---+---+---++"
        size = len(self._board[0])
        spacers = (spacer if (i+1) % 3 else spacer.replace('-','=') for i in range(size))
        fmt = "|| {} | {} | {} || {} | {} | {} || {} | {} | {} ||"

        formats = (fmt.format(*(cell or ' ' for cell in line)) for line in self._board)
        return spacer.replace('-','=') + '\n' + '\n'.join(interleave(formats, spacers))

    def remove(self, xy, number):
        self.stored_numbers[xy].remove(number)
        if not self.stored_numbers[xy]:
            del self.stored_numbers[xy]
            self[xy] = None

    def store(self, xy, number):
        self[xy] = 0
        numbers = self.stored_numbers.setdefault(xy, [])
        if number in numbers:
            return

        numbers.append(number)

    def clear(self):
        print(self._placed_numbers)
        for x, y in iter_except(self._placed_numbers.pop, KeyError):
            self._board[y][x] = None
        self.stored_numbers.clear()

    def is_full(self):
        return None not in itertools.chain.from_iterable(self._board)

    def is_correct(self):
        return self._board == self._solved

    @classmethod
    def beginner(cls):
        """Returns a sudoku board suitable for beginners"""
        return cls(holes=36)

    @classmethod
    def intermediate(cls):
        """Returns a sudoku board suitable for intermediate players"""
        return cls(holes=random.randint(45, 54))

    @classmethod
    def expert(cls):
        """Returns a sudoku board suitable for experts"""
        return cls(holes=random.randint(59, 62))

    @classmethod
    def minimum(cls):
        """Returns a sudoku board with the minimum amount of clues needed
        to achieve a unique solution.
        """
        return cls(holes=BOARD_SIZE - 17)


_markers = [chr(i) for i in range(0x1f1e6, 0x1f1ef)]
_top_row = '  '.join(map(' '.join, grouper(3, _markers)))
_top_row = '\N{SOUTH EAST ARROW}  ' + _top_row
_letters = 'abcdefghi'

class UnicodeBoard(Board):
    def __str__(self):
        return '\n'.join("{0}  {1} {2} {3}  {4} {5} {6}  {7} {8} {9}"
                         .format(_markers[i], *(f'{cell}\u20e3' if cell else
                                   '\N{BLACK LARGE SQUARE}' if cell is None else
                                   '\N{INPUT SYMBOL FOR NUMBERS}' for cell in line),
                                '\N{WHITE SMALL SQUARE}')
                         + '\n' * (((i + 1) % 3 == 0))
                         for i, line in enumerate(self._board))


class Level(enum.Enum):
    beginner = enum.auto()
    intermediate = enum.auto()
    expert = enum.auto()
    minimum = enum.auto()

    def __str__(self):
        return self.name.title()

    @classmethod
    async def convert(cls, ctx, arg):
        lowered = arg.lower()
        try:
            return cls[lowered]
        except KeyError:
            raise commands.BadArgument(f'No level called {arg}.') from None


class State(enum.Enum):
    default = enum.auto()
    on_help = enum.auto()

class SudokuSession(BaseReactionPaginator):
    def __init__(self, ctx, board, level):
        self.context = ctx
        self.board = board
        self._message = None
        self._header = f'Sudoku - {level}'
        self._state = State.default
        self._completed = False
        self._runner = None
        self._screen = (discord.Embed()
                       .set_author(name=self._header)
                       .add_field(name='Player', value=str(ctx.author))
                       .add_field(name='\u200b', value='Stuck? Click the \N{INFORMATION SOURCE} for help', inline=False)
                       )

    def check_message(self, message):
        return (self._state == State.default
                and message.channel == self.ctx.channel
                and message.author == self.ctx.author)

    @staticmethod
    def parse_message(string):
        x, y, number, = string.lower().split()

        if number == 'clear':
            number = None
        else:
            number = int(number)
            if not 1 <= number <= 9:
                raise ValueError("number must be between 1 and 9")

        return _letters.index(x), _letters.index(y), number

    def edit_screen(self):
        self._screen.description = f'{_top_row}\n{self.board}'

    async def _loop(self):
        self.edit_screen()
        self._message = await self.ctx.send(embed=self._screen)
        await self.add_buttons()

        while True:
            try:
                message = await self.ctx.bot.wait_for('message', timeout=120, check=self.check_message)
            except asyncio.TimeoutError:
                if self._state == State.default:
                    raise
                continue

            try:
                x, y, number = self.parse_message(message.content)
            except ValueError:
                continue

            try:
                self.board[x, y] = number
            except (IndexError, ValueError):
                continue

            with suppress(discord.NotFound):
                await message.delete()

            self.edit_screen()
            await self._message.edit(embed=self._screen)

    async def run(self):
        try:
            with self.ctx.bot.temp_listener(self.on_reaction_add):
                self._runner = asyncio.ensure_future(self._loop())
                await self._runner
        finally:
            if not self._completed:
                self._screen = self._message.embeds[0]
                self._screen.colour = 0

            await self._message.edit(embed=self._screen)
            await self._message.clear_reactions()

    @page('\N{WHITE HEAVY CHECK MARK}')
    async def check(self):
        """Check to see if your the board you have is correct."""
        if not self.board.is_full():
            self._screen.set_author(name="This board isn't even remotely done!")
            self._screen.colour = 0xFF0000
        elif self.board.is_correct():
            self._screen.set_author(name="Sudoku complete!")
            self._completed = True
            self._runner.cancel()
        else:
            self._screen.set_author(name="Sorry, it's not correct :(")
            self._screen.colour = 0xFF0000

        await self._message.edit(embed=self._screen)
        await asyncio.sleep(10)
        self._screen.set_author(name=self._header)
        self._screen.colour = self.ctx.bot.colour
        await self._message.edit(embed=self._screen)

    @page('\N{INPUT SYMBOL FOR NUMBERS}')
    async def default(self):
        """Go back to the game"""
        self._state = State.default
        await self._message.edit(embed=self._screen)

    @page('\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}')
    async def reset(self):
        """Reset the board. In case you badly mess up or something."""
        self.board.clear()
        self.edit_screen()
        await self._message.edit(embed=self._screen)

    @page('\N{INFORMATION SOURCE}')
    async def help_page(self):
        """Show this page (you knew that already)"""
        self._state = State.on_help
        help_text = textwrap.dedent('''
        The objective is to fill a 9×9 grid with digits so that each column,
        each row, and each of the nine 3×3 subgrids that compose the grid
        (also called "boxes", "blocks", or "regions") contains all of the
        digits from 1 to 9.

        This basically means no row, column, or block should have more than
        one of the same number.
        \u200b
        ''')

        input_field = textwrap.dedent('''
        To make a move, send a message in this format:
        ```
        <row> <column> <number>
        ```
        `row` and `column` can be from `A-I`. While the number must be from `1-9`

        When you think you're done, click the \N{WHITE HEAVY CHECK MARK} to check your board.
        Keep in mind this will only work once every number has been put in.
        \u200b
        ''')

        embed =  (discord.Embed(colour=self.ctx.bot.colour, description=help_text)
                 .set_author(name='Welcome to Sudoku!')
                 .add_field(name='How to play', value=input_field)
                 .add_field(name='Reaction Button Reference', value=self.reaction_help)
                 )

        await self._message.edit(embed=embed)

    @page('\N{BLACK SQUARE FOR STOP}')
    async def stop(self):
        """Stop the game"""
        self._runner.cancel()

    @property
    def context(self):
        return self.ctx

    @context.setter
    def context(self, value):
        self.ctx = value

    async def on_reaction_add(self, reaction, user):
        if self._check_reaction(reaction, user):
            await getattr(self, self._reaction_map[reaction.emoji])()


class Sudoku(Cog):
    def __init__(self):
        self.manager = SessionManager()

    @commands.command()
    async def sudoku(self, ctx, difficulty: Level = Level.beginner):
        """Starts game of Sudoku"""
        if self.manager.session_exists(ctx.author):
            return await ctx.send('no')

        board = getattr(UnicodeBoard, difficulty.name.lower())()
        with self.manager.temp_session(ctx.author, SudokuSession(ctx, board, difficulty)) as inst:
            await inst.run()

    async def sudoku_load(self, ctx):
        pass

    @sudoku.error
    async def sudoku_error(self, ctx, error):
        cause = error.__cause__
        if isinstance(cause, asyncio.TimeoutError):
            await ctx.send(f'{ctx.author.mention} You took too long!')


def setup(bot):
    bot.add_cog(Sudoku())