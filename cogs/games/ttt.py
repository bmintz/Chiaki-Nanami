import asyncio
import discord
import enum
import itertools
import random

from collections import namedtuple
from more_itertools import first_true

from . import errors
from .bases import TwoPlayerGameCog

from ..utils.context_managers import temp_message


class Tile(enum.Enum):
    BLANK = '\N{BLACK LARGE SQUARE}'
    X = '\N{CROSS MARK}'
    O = '\N{HEAVY LARGE CIRCLE}'

    def __str__(self):
        return self.value


_horizontal_divider = '\N{BOX DRAWINGS LIGHT HORIZONTAL}'


def _is_winning_line(line):
    line = set(line)
    return len(line) == 1 and Tile.BLANK not in line

class Board:
    def __init__(self, size=3):
        self._board = [[Tile.BLANK] * size for _ in range(size)]
        self._divider = ' | ' * (size <= 5)

    def __repr__(self):
        return f'{self.__class__.__name__}(size={self.size})'

    def __str__(self):
        return f'\n'.join((' ' + self._divider.join(map(str, row)))
                                     for row in self.rows())

    def place(self, x, y, tile):
        if self._board[y][x] != Tile.BLANK:
            raise ValueError(f"tile {x} {y} is not empty")
        self._board[y][x] = tile

    def is_full(self):
        return Tile.BLANK not in itertools.chain.from_iterable(self._board)

    def will_tie(self):
        return all(len(set(line) - {Tile.BLANK}) == 2
                   for line in itertools.chain(self.rows(), self.columns(), self.diagonals()))

    def mark_winning_line(self):
        """Shows the winning line (for visualization)"""
        assert self.winner, "board doesn't have a winner yet"

        def mark(coords, tile):
            winning_tile = '\U0001f17e' if tile == Tile.O else '\U0000274e'
            for x, y in coords:
                self._board[y][x] = winning_tile

        for i, line in enumerate(self.rows()):
            if _is_winning_line(line):
                mark(zip(range(self.size), itertools.repeat(i)), line[0])
                return

        for i, line in enumerate(self.columns()):
            if _is_winning_line(line):
                mark(zip(itertools.repeat(i), range(self.size)), line[0])
                return

        d, ad = self.diagonals()
        if _is_winning_line(d):
            coords = ((i, i) for i in range(self.size))
            mark(coords, d[0])
        elif _is_winning_line(ad):
            coords = ((~i, i) for i in range(self.size))
            mark(coords, ad[0])

    @property
    def winner(self):
        """Returns the winner of a given board configuration. None if there is no winner.

        A winner in tic-tac-toe fills at least one row, column, or diagonal
        with their tile.
        """
        lines = itertools.chain(self.rows(), self.columns(), self.diagonals())
        return first_true(lines, (None, ), _is_winning_line)[0]

    @property
    def size(self):
        """Returns the size of the board"""
        return len(self._board)

    def rows(self):
        """Returns an iterator of the board's rows"""
        return map(tuple, self._board)

    def columns(self):
        """Returns an iterator of the board's columns"""
        return zip(*self._board)

    def diagonals(self):
        """Returns an iterator of the board's diagonals.
        First the main diagonal, then the anti-diagonal.
        """

        yield tuple(self._board[i][i] for i in range(self.size))
        yield tuple(self._board[i][~i] for i in range(self.size))


Player = namedtuple('Player', 'user symbol')
Stats = namedtuple('Stats', 'winner turns')


_draw_warning = '''
\N{WARNING SIGN} **Warning**
This game will be a tie. Type `draw` to request a draw.
Continuing this game will most likely be a waste of time.
'''


class TicTacToeSession:
    def __init__(self, ctx, opponent):
        self.ctx = ctx
        self.board = Board(ctx._ttt_size)
        self.opponent = opponent

        xo = random.sample((Tile.X, Tile.O), 2)
        self.players = random.sample(list(map(Player, (self.ctx.author, self.opponent), xo)), 2)
        self._current = None
        self._runner = None

        size = self.ctx._ttt_size
        help_text = ('Type the column and the row in the format `column row` to make your move!\n'
                     'Or `quit` to stop the game (you will lose though).')
        player_field = '\n'.join(itertools.starmap('{1} = **{0}**'.format, self.players))
        self._game_screen = (discord.Embed(colour=0x00FF00)
                            .set_author(name=f'Tic-Tac-toe - {size} x {size}')
                            .add_field(name='Players', value=player_field)
                            .add_field(name='Current Player', value=None, inline=False)
                            .add_field(name='Instructions', value=help_text)
                            )

    def get_coords(self,string):
        lowered = string.lower()
        if lowered in {'quit', 'stop'}:
            raise errors.RageQuit

        if lowered == 'draw':
            if self.board.will_tie():
                raise errors.DrawRequested
            raise ValueError("Game is not drawn yet.")

        x, y, = string.split()
        ix, iy = int(x), int(y)

        if not ix > 0 < iy:
            raise ValueError("Only positive numbers are allowed")

        return ix - 1, iy - 1

    def _check_message(self, m):
        return m.channel == self.ctx.channel and m.author.id == self._current.user.id

    async def get_input(self):
        while True:
            message = await self.ctx.bot.wait_for('message', timeout=120, check=self._check_message)
            try:
                coords = self.get_coords(message.content)
            except ValueError:
                continue
            else:
                await message.delete()
                return coords

    def _update_display(self):
        screen = self._game_screen
        user = self._current.user

        screen.description = f'**Current Board:**\n\n{self.board}'
        if self.board.will_tie():
            screen.description = _draw_warning + screen.description

        screen.set_thumbnail(url=user.avatar_url)
        screen.set_field_at(1, name='Current Move', value=str(user), inline=False)

    async def _process_draw(self, other):
        confirm_options = ['\N{WHITE HEAVY CHECK MARK}', '\N{CROSS MARK}']
        desc = 'Press {0} to accept the draw.\nPress {1} to decline.'.format(*confirm_options)
        embed = (discord.Embed(description=desc)
                .set_author(name=f'{self._current.user} has requested a draw.')
                )

        message = await self.ctx.send(embed=embed)
        for emoji in confirm_options:
            await message.add_reaction(emoji)

        def confirm_check(reaction, user):
            return (other == user
                    and reaction.message.id == message.id
                    and reaction.emoji in confirm_options)

        react, member = await self.ctx.bot.wait_for('reaction_add', check=confirm_check)
        return react.emoji == confirm_options[0]

    async def _loop(self):
        cycle = itertools.cycle(self.players)
        for turn, self._current in enumerate(cycle, start=1):
            user, tile = self._current
            self._update_display()
            async with temp_message(self.ctx, content=f'{user.mention} It is your turn.',
                                    embed=self._game_screen) as m:
                while True:
                    try:
                        x, y = await self.get_input()
                    except (asyncio.TimeoutError, errors.RageQuit):
                        return Stats(next(cycle), turn)
                    except errors.DrawRequested:
                        if await self._process_draw(next(cycle).user):
                            return Stats(None, turn)
                        break

                    try:
                        self.board.place(x, y, tile)
                    except (ValueError, IndexError):
                        pass
                    else:
                        break

                winner = self.winner
                if winner or self.board.is_full():
                    return Stats(winner, turn)

    async def run(self):
        try:
            return await self._loop()
        finally:
            if self.winner:
                self.board.mark_winning_line()

            self._update_display()
            self._game_screen.set_author(name='Game ended.')
            self._game_screen.colour = 0
            await self.ctx.send(embed=self._game_screen)

    @property
    def winner(self):
        return discord.utils.get(self.players, symbol=self.board.winner)


BOARD_SIZE_EMOJIS = list(map('{}\U000020e3'.format, range(3, 8))) + ['\N{BLACK SQUARE FOR STOP}']
_is_valid_board_size_emoji = frozenset(BOARD_SIZE_EMOJIS).__contains__


class TicTacToe(TwoPlayerGameCog, name='Tic-Tac-Toe', game_cls=TicTacToeSession, aliases=['ttt']):
    @staticmethod
    async def get_board_size(ctx):
        embed = (discord.Embed(colour=0x00FF00, description='Click one of the reactions below!')
                .set_author(name=f'Please enter the size of the board {ctx.author}')
                )

        async def add_reactions(message):
            for emoji in BOARD_SIZE_EMOJIS:
                await message.add_reaction(emoji)

        async with temp_message(ctx, embed=embed) as message:
            future = asyncio.ensure_future(add_reactions(message))

            def check(react, user):
                return (react.message.id == message.id
                        and user.id == ctx.author.id
                        and _is_valid_board_size_emoji(react.emoji)
                        )

            try:
                react, user = await ctx.bot.wait_for('reaction_add', check=check)
                if react.emoji == '\N{BLACK SQUARE FOR STOP}':
                    raise errors.RageQuit(f'{ctx.author} cancelled selecting the board size')
                return int(react.emoji[0])
            finally:
                if not future.done():
                    future.cancel()

    def _create_invite(self, ctx, member):
        size = ctx._ttt_size
        return (super()._create_invite(ctx, member)
               .set_footer(text=f'Board size: {size} x {size}'))

    async def _invite_member(self, ctx, member):
        ctx._ttt_size = await self.get_board_size(ctx)
        await super()._invite_member(ctx, member)


def setup(bot):
    bot.add_cog(TicTacToe(bot))