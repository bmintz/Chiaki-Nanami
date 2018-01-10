import asyncio
import itertools
import random
from collections import namedtuple

import discord
from more_itertools import chunked

from . import errors
from .bases import TwoPlayerGameCog
from ..utils.context_managers import temp_message
from ..utils.formats import escape_markdown

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
WINNING_TILES = ['\U0000274e', '\U0001f17e']
WINNING_TILE_MAP = dict(zip(TILES, WINNING_TILES))
DIVIDER = '\N{BOX DRAWINGS LIGHT HORIZONTAL}' * SIZE

class Board:
    def __init__(self):
        self._board = [None] * SIZE

    def __str__(self):
        return f'\n{DIVIDER}\n'.join(
            ' | '.join(c or f'{i}\u20e3' for i, c in chunk)
            for chunk in chunked(enumerate(self._board, 1), 3)
        )

    def place(self, x, thing):
        self._board[x] = thing

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


Player = namedtuple('Player', 'user symbol')
Stats = namedtuple('Stats', 'winner turns')


class TicTacToeSession:
    def __init__(self, ctx, opponent):
        self.ctx = ctx
        self.opponent = opponent

        xo = random.sample(TILES, 2)
        self._players = list(map(Player, (self.ctx.author, self.opponent), xo))
        self._turn = random.random() > 0.5

        self._board = Board()
        self._stopped = False

        self._game_screen = discord.Embed(colour=0x00FF00)

    def _check_message(self, m):
        if not (m.channel == self.ctx.channel and m.author == self.current.user):
            return False

        string = m.content
        lowered = string.lower()

        if lowered in {'quit', 'stop'}:
            self._stopped = True
            return True

        return string.isdigit() and 1 <= int(string) <= SIZE

    async def get_input(self):
        while True:
            message = await self.ctx.bot.wait_for('message', timeout=120, check=self._check_message)
            await message.delete()

            if self._stopped:
                raise errors.RageQuit

            return int(message.content) - 1

    def _update_display(self):
        screen = self._game_screen

        formats = [
            f'{p.symbol} = {escape_markdown(str(p.user))}'
            for p in self._players
        ]
        formats[self._turn] = f'**{formats[self._turn]}**'
        joined = '\n'.join(formats)

        screen.description = f'{self._board}\n\u200b\n{joined}'
        screen.set_author(name='Tic-Tac-toe', icon_url=self.current.user.avatar_url)

    async def _loop(self):
        for counter in itertools.count(1):
            user, tile = self.current
            self._update_display()

            async with temp_message(self.ctx, content=f'{user.mention} It is your turn.',
                                    embed=self._game_screen):
                try:
                    spot = await self.get_input()
                except (asyncio.TimeoutError, errors.RageQuit):
                    return Stats(self._players[not self._turn], counter)

                self._board.place(spot, tile)

                winner = self.winner
                if winner or self._board.is_full():
                    return Stats(winner, counter)

            self._turn = not self._turn

    async def run(self):
        try:
            return await self._loop()
        finally:
            if self.winner:
                self._board.mark()

            self._update_display()
            self._game_screen.set_author(name='Game ended.')
            self._game_screen.colour = 0
            await self.ctx.send(embed=self._game_screen)

    @property
    def winner(self):
        return discord.utils.get(self._players, symbol=self._board.winner())

    @property
    def current(self):
        return self._players[self._turn]


class TicTacToe(TwoPlayerGameCog, name='Tic-Tac-Toe', game_cls=TicTacToeSession, aliases=['ttt']):
    pass

def setup(bot):
    bot.add_cog(TicTacToe(bot))
