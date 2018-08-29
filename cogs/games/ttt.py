import discord
from more_itertools import chunked

from .bases import Status, TwoPlayerGameCog, TwoPlayerSession
from ..utils.context_managers import temp_message
from ..utils.formats import escape_markdown
from ..utils.misc import emoji_url

SIZE = 9
WIN_COMBINATIONS = [
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
]

TILES = ['\N{CROSS MARK}', '\N{HEAVY LARGE CIRCLE}']
TILE_TURNS = dict(zip(TILES, [False, True]))
WINNING_TILES = ['\U0000274e', '\U0001f17e']
WINNING_TILE_MAP = dict(zip(TILES, WINNING_TILES))
DIVIDER = '\N{BOX DRAWINGS LIGHT HORIZONTAL}' * SIZE

class Board:
    def __init__(self):
        self._board = [None] * SIZE
        self._turn = False

    def __str__(self):
        return f'\n{DIVIDER}\n'.join(
            ' | '.join(c or f'{i}\u20e3' for i, c in chunk)
            for chunk in chunked(enumerate(self._board, 1), 3)
        )

    def place(self, x):
        if self._board[x] is not None:
            raise IndexError(f'{x} is already occupied')
        self._board[x] = TILES[self._turn]
        self._turn = not self._turn

    def is_full(self):
        return None not in self._board

    def _winning_line(self):
        board = self._board
        for a, b, c in WIN_COMBINATIONS:
            if board[a] == board[b] == board[c] is not None:
                return a, b, c
        return None

    def winner(self):
        result = self._winning_line()
        if not result:
            return result
        return self._board[result[0]]

    def mark(self):
        result = self._winning_line()
        if not result:
            return

        tile = WINNING_TILE_MAP[self._board[result[0]]]
        for r in result:
            self._board[r] = tile


# icons
FOREFIT_ICON = emoji_url('\N{WAVING WHITE FLAG}')
TIMEOUT_ICON = emoji_url('\N{ALARM CLOCK}')

class TicTacToeSession(TwoPlayerSession, board_factory=Board, move_pattern=r'^(\d)$'):
    def __init__(self, ctx, opponent):
        super().__init__(ctx, opponent)
        self._display = discord.Embed(colour=0x00FF00)

    def _push_move(self, index):
        index = int(index[0])
        if not 1 <= index <= SIZE:
            raise ValueError(f'must be 1 <= space <= {SIZE}')

        self._board.place(index - 1)

    def _is_game_over(self):
        return self._board.winner() or self._board.is_full()

    def current(self):
        return self._players[self._board._turn]

    async def _update_display(self):
        screen = self._display
        user = self.current()
        winner = self._board.winner()
        turn = self._board._turn

        # How can I make this cleaner...
        formats = [
            f'{symbol} = {escape_markdown(str(user))}'
            for symbol, user in zip(TILES, self._players)
        ]

        if not winner:
            formats[turn] = f'**{formats[turn]}**'
        else:
            self._board.mark()

        joined = '\n'.join(formats)

        screen.description = f'{self._board}\n\u200b\n{joined}'

        if winner:
            user = self._players[TILE_TURNS[winner]]
            screen.set_author(name=f'{user} wins!', icon_url=user.avatar_url)
        elif self._status is Status.QUIT:
            screen.colour = 0
            screen.set_author(name=f'{user} forefited...', icon_url=FOREFIT_ICON)
        elif self._status is Status.TIMEOUT:
            screen.colour = 0
            screen.set_author(name=f'{user} ran out of time...', icon_url=TIMEOUT_ICON)
        elif self._board.is_full():
            screen.colour = 0
            screen.set_author(name="It's a tie!")
        else:
            screen.set_author(name='Tic-Tac-Toe', icon_url=user.avatar_url)

    def _send_message(self):
        return temp_message(
            self._ctx,
            content=f'{self.current().mention} It is your turn.',
            embed=self._display
        )


class TicTacToe(TwoPlayerGameCog, name='Tic-Tac-Toe', game_cls=TicTacToeSession, aliases=['ttt']):
    pass

def setup(bot):
    bot.add_cog(TicTacToe(bot))
