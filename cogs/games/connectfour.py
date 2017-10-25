import asyncio
import discord
import enum
import itertools
import random

from collections import namedtuple
from more_itertools import first_true, one, windowed

from . import errors
from .bases import TwoPlayerGameCog

from ..utils.context_managers import temp_message

NUM_ROWS = 6
NUM_COLS = 7
WINNING_LENGTH = 4


def _diagonals(matrix, n):
    h, w = len(matrix), len(matrix[0])
    return (tuple(matrix[y + d][x + d] for d in range(n))
            for x, y in itertools.product(range(w - n + 1), range(h - n + 1)))

def _anti_diagonals(matrix, n):
    h, w = len(matrix), len(matrix[0])
    return (tuple(matrix[y + d][~x - d] for d in range(n))
            for x, y in itertools.product(range(w - n + 1), range(h - n + 1)))


class Tile(enum.Enum):
    NONE = '\N{MEDIUM BLACK CIRCLE}'
    X = '\N{LARGE RED CIRCLE}'
    O = '\N{LARGE BLUE CIRCLE}'

    def __str__(self):
        return self.value


class Board:
    def __init__(self):
        self._board = [[Tile.NONE] * NUM_ROWS for _ in range(NUM_COLS)]

    def __str__(self):
        fmt = ''.join(itertools.repeat('{}', NUM_COLS))
        return '\n'.join(map(fmt.format, *map(reversed, self._board)))

    def is_full(self):
        return Tile.NONE not in itertools.chain.from_iterable(self._board)

    def place(self, column, piece):
        column = self._board[column]
        column[column.index(Tile.NONE)] = piece

    # TODO: Mark the winning line. This is significantly harder than TTT

    def horizontals(self):
        """Returns an iterator of all the possible horizontal lines of four."""
        return itertools.chain.from_iterable(windowed(row, 4) for row in zip(*self._board))

    def verticals(self):
        """Returns an iterator of all the possible vertical lines of four."""
        return itertools.chain.from_iterable(windowed(column, 4) for column in self._board)

    def diagonals(self):
        """Returns an iterator of all the possible diagonal lines of four."""
        return _diagonals(self._board, 4)

    def anti_diagonals(self):
        """Returns an iterator of all the possible diagonal lines of four."""
        return _anti_diagonals(self._board, 4)

    @property
    def winner(self):
        def is_full(line):
            line = set(line)
            return len(line) == 1 and Tile.NONE not in line
        lines = itertools.chain(self.horizontals(), self.verticals(),
                                self.diagonals(), self.anti_diagonals())
        return first_true(lines, (None, ), is_full)[0]


Player = namedtuple('Player', 'user symbol')
Stats = namedtuple('Stats', 'winner turns')


class ConnectFourSession:
    def __init__(self, ctx, opponent):
        self.ctx = ctx
        self.board = Board()
        self.opponent = opponent

        xo = random.sample((Tile.X, Tile.O), 2)
        self.players = random.sample(list(map(Player, (self.ctx.author, self.opponent), xo)), 2)
        self._current = None
        self._runner = None

        player_field = '\n'.join(itertools.starmap('{1} = **{0}**'.format, self.players))
        instructions = ('Type the number of the column to play!\n'
                        'Or `quit` to stop the game (you will lose though).')
        self._game_screen = (discord.Embed(colour=0x00FF00)
                            .set_author(name=f'Connect 4 - {self.ctx.author} vs {self.opponent}')
                            .add_field(name='Players', value=player_field)
                            .add_field(name='Current Player', value=None, inline=False)
                            .add_field(name='Instructions', value=instructions)
                            )

    @staticmethod
    def get_column(string):
        lowered = string.lower()
        if lowered in {'quit', 'stop'}:
            raise errors.RageQuit

        if lowered in {'help', 'h'}:
            return 'h'

        column = int(one(string))
        if not 1 <= column <= 7:
            raise ValueError('must be 1 <= column <= 7')
        return column - 1

    def _check_message(self, m):
        return m.channel == self.ctx.channel and m.author.id == self._current.user.id

    async def get_input(self):
        while True:
            message = await self.ctx.bot.wait_for('message', timeout=120, check=self._check_message)
            try:
                coords = self.get_column(message.content)
            except (ValueError, IndexError):
                continue
            else:
                await message.delete()
                return coords

    def _update_display(self):
        screen = self._game_screen
        user = self._current.user

        numbers = ''.join(f'{i}\U000020e3' for i in range(1, NUM_COLS + 1))
        screen.description = f'**Current Board:**\n\n{numbers}\n{self.board}'
        screen.set_thumbnail(url=user.avatar_url)
        screen.set_field_at(1, name='Current Move', value=str(user), inline=False)

    async def _loop(self):
        cycle = itertools.cycle(self.players)
        for turn, self._current in enumerate(cycle, start=1):
            user, tile = self._current
            self._update_display()
            async with temp_message(self.ctx, embed=self._game_screen) as m:
                while True:
                    try:
                        column = await self.get_input()
                    except (asyncio.TimeoutError, errors.RageQuit):
                        return Stats(next(cycle), turn)

                    if column == 'h':
                        await self._send_help_embed()
                        continue
                    try:
                        self.board.place(column, tile)
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
            self._update_display()
            self._game_screen.set_author(name='Game ended.')
            self._game_screen.colour = 0
            await self.ctx.send(embed=self._game_screen)

    @property
    def winner(self):
        return discord.utils.get(self.players, symbol=self.board.winner)

class Connect4(TwoPlayerGameCog, name='Connect 4', game_cls=ConnectFourSession, aliases=['con4']):
    def _make_invite_embed(self, ctx, member):
        return (super()._make_invite_embed(ctx, member)
               .set_footer(text='Board size: 7 x 6'))


def setup(bot):
    bot.add_cog(Connect4(bot))