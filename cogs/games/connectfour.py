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
from ..utils.formats import escape_markdown

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
        return self.top_row + '\n' + '\n'.join(map(fmt.format, *map(reversed, self._board)))

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

# XXX: Should be refactored along with tic-tac-toe
class ConnectFourSession:
    def __init__(self, ctx, opponent):
        self.ctx = ctx
        self.opponent = opponent

        xo = random.sample((Tile.X, Tile.O), 2)
        self._players = random.sample(list(map(Player, (self.ctx.author, self.opponent), xo)), 2)
        self._stopped = False
        self._turn = random.random() > 0.5
        self._runner = None
        self._board = Board()

        instructions = ('Type the number of the column to play!\n'
                        'Or `quit` to stop the game (you will lose though).')

        self._game_screen = (discord.Embed(colour=0x00FF00)
                             .set_author(name=f'Connect 4')
                             .add_field(name='Instructions', value=instructions)
                             )

    def _check(self, m):
        current = self.current
        if not (m.channel == self.ctx.channel and m.author.id == current.user.id):
            return

        lowered = m.content.lower()
        if lowered in {'quit', 'stop'}:
            self._stopped = True
            return True

        if not lowered.isdigit():
            return

        try:
            self._board.place(int(lowered) - 1, current.symbol)
        except (ValueError, IndexError):
            return
        else:
            return True

    async def wait_for_player_move(self):
        message = await self.ctx.bot.wait_for('message', timeout=60, check=self._check)
        await message.delete()
        if self._stopped:
            raise errors.RageQuit

    def _update_display(self):
        screen = self._game_screen

        formats = [
            f'{p.symbol} = {escape_markdown(str(p.user))}'
            for p in self._players
        ]
        formats[self._turn] = f'**{formats[self._turn]}**'
        joined = '\n'.join(formats)

        b = self._board
        screen.description = f'{b}\n\u200b\n{joined}'

    async def _loop(self):
        for turn in itertools.count(1):
            user, tile = self.current
            self._update_display()

            async with temp_message(self.ctx, embed=self._game_screen):
                try:
                    await self.wait_for_player_move()
                except (asyncio.TimeoutError, errors.RageQuit):
                    return Stats(self._players[not self._turn], turn)

                winner = self.winner
                if winner or self._board.is_full():
                    if winner:
                        self._board.mark_winning_lines()
                    return Stats(winner, turn)
            self._turn = not self._turn

    async def run(self):
        try:
            return await self._loop()
        finally:
            self._update_display()
            self._game_screen.set_author(name='Game ended.')
            self._game_screen.colour = 0
            await self.ctx.send(embed=self._game_screen)

    @property
    def current(self):
        return self._players[self._turn]

    @property
    def winner(self):
        return discord.utils.get(self._players, symbol=self._board.winner)

class Connect4(TwoPlayerGameCog, name='Connect 4', game_cls=ConnectFourSession, aliases=['con4']):
    def _make_invite_embed(self, ctx, member):
        return (super()._make_invite_embed(ctx, member)
                .set_footer(text='Board size: 7 x 6')
                )


def setup(bot):
    bot.add_cog(Connect4(bot))