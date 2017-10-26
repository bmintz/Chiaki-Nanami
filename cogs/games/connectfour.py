import asyncio
import discord
import enum
import itertools
import random

from collections import namedtuple
from more_itertools import first_true, locate, one, windowed

from . import errors
from .bases import TwoPlayerGameCog

from ..utils.context_managers import temp_message

NUM_ROWS = 6
NUM_COLS = 7
WINNING_LENGTH = 4


def _board_iterator_helper(num_cols, num_rows, length):
    cols, rows = range(num_cols), range(num_rows)
    for col in cols:
        yield from (tuple((col, r) for r in row) for row in windowed(rows, length))

    for row in rows:
        yield from (tuple((c, row) for c in col) for col in windowed(cols, WINNING_LENGTH))

    for row_diag, col_diag in itertools.product(range(num_rows - length + 1), range(num_cols - length + 1)):
        yield tuple((col_diag + d, row_diag + d) for d in range(length))
        yield tuple((col_diag + d, ~row_diag - d) for d in range(length))

_default_indices = list(_board_iterator_helper(NUM_COLS, NUM_ROWS, WINNING_LENGTH))


class Tile(enum.Enum):
    NONE = '\N{MEDIUM BLACK CIRCLE}'
    X = '\N{LARGE RED CIRCLE}'
    O = '\N{LARGE BLUE CIRCLE}'

    def __str__(self):
        return self.value


def _is_full(line):
    line = set(line)
    return len(line) == 1 and Tile.NONE not in line


_winning_tiles = {
    Tile.X: '\N{HEAVY BLACK HEART}',
    Tile.O: '\N{BLUE HEART}'
}


class Board:
    def __init__(self):
        self._board = [[Tile.NONE] * NUM_ROWS for _ in range(NUM_COLS)]
        self._last_column = None

    def __str__(self):
        fmt = ''.join(itertools.repeat('{}', NUM_COLS))
        return '\n'.join(map(fmt.format, *map(reversed, self._board)))

    def is_full(self):
        return Tile.NONE not in itertools.chain.from_iterable(self._board)

    def place(self, column, piece):
        board_column = self._board[column]
        board_column[board_column.index(Tile.NONE)] = piece
        self._last_column = column

    def mark_winning_lines(self):
        b = self._board

        lines = (tuple(b[c][r] for c, r in line) for line in _default_indices)
        for line_idx in locate(lines, _is_full):
            indices = _default_indices[line_idx]
            winner = b[indices[0][0]][indices[0][1]]
            emoji = _winning_tiles[winner]

            for c, r in indices:
                # TODO: Custom emojis for tiles?
                b[c][r] = emoji

    @property
    def winner(self):
        lines = (tuple(self._board[c][r] for c, r in line) for line in _default_indices)
        return first_true(lines, (None, ), _is_full)[0]

    @property
    def top_row(self):
        numbers = [f'{i}\U000020e3' for i in range(1, NUM_COLS + 1)]
        if self._last_column is not None:
            numbers[self._last_column] = '\U000023ec'
        return ''.join(numbers)


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

        b = self.board
        screen.description = f'**Current Board:**\n\n{b.top_row}\n{b}'
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
                    if winner:
                        self.board.mark_winning_lines()
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