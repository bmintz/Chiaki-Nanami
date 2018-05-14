import itertools
import string
from collections import Counter

from more_itertools import flatten, interleave_longest


def _xy_to_i(xy):
    x, y = xy
    return ord(x) - ord('a'), int(y) - 1


class Board:
    def __init__(self, width=3, height=3):
        self._vertical = [[None] * (width + 1) for _ in range(height)]
        self._horizontal = [[None] * width for _ in range(height + 1)]
        self._boxes = [[None] * width for _ in range(height)]
        self._turn = 0

    def __repr__(self):
        return f'{self.__class__.__name__}(width={self.width!r}, height={self.height!r})'

    def __str__(self):
        v, h = self._vertical, self._horizontal
        v_size = len(self._vertical[0])
        h_fmt = '{}'.join('o' * v_size)
        v_fmt = ' {} '.join(['{}'] * v_size)

        letters = '   '.join(string.ascii_uppercase[:v_size])
        horizontals = (
            f'{i} {h_fmt.format(*(" -"[x is not None] * 3 for x in row))}'
            for i, row in enumerate(h, 1)
        )

        verticals = ('  ' + v_fmt.format(*interleave_longest(
            (' |'[r is not None] for r in row),
            (s + 1 if s is not None else ' ' for s in score_row),
        )) for score_row, row in zip(self._boxes, v))

        return '  ' + letters + '\n' + '\n'.join(interleave_longest(horizontals, verticals))

    def _check_legality(self, p1, p2):
        (x1, y1), (x2, y2) = p1, p2
        if y1 != y2 and x1 != x2:
            raise ValueError('Points must form a horizontal or vertical line')

        if abs(x1 - x2) != 1 and abs(y1 - y2) != 1:
            raise ValueError('Line is too long. Please make it shorter.')

        w, h = self.width, self.height
        if x1 > w or x2 > w or y1 > h or y2 > h:
            raise ValueError('This line goes too far')

    def _check_and_set_squares(self, x, y):
        b = self._boxes
        if self._boxes[y][x] is not None:
            return 0

        v, h = self._vertical, self._horizontal
        if all(x is not None for x in [v[y][x], v[y][x + 1], h[y][x], h[y + 1][x]]):
            b[y][x] = self._turn
            return 1
        return 0

    def _make_line(self, p1, p2):
        self._check_legality(p1, p2)

        (x1, y1), (x2, y2) = p1, p2
        x = min(x1, x2)
        y = min(y1, y2)

        if x1 == x2:  # vertical
            selected = self._vertical
            off_x, off_y = x - 1, y
        else:         # horizontal
            selected = self._horizontal
            off_x, off_y = x, y - 1

        if selected[y][x] is not None:
            raise ValueError('Someone already made that line. Please make another.')

        selected[y][x] = self._turn

        filled = 0
        w, h = self.width, self.height
        if 0 <= x < w and 0 <= y < h:
            filled = self._check_and_set_squares(x, y)
        if 0 <= off_x < w and 0 <= off_y < h:
            filled += self._check_and_set_squares(off_x, off_y)

        if not filled:
            self._turn = not self._turn

    def move(self, move):
        """Parse a move, and update accordingly.

        For example, a1a2 will make a line at (0, 0), (0, 1)

        This assumes the input is well-formed.
        """
        p1 = _xy_to_i(move[:2])
        p2 = _xy_to_i(move[2:4])
        self._make_line(p1, p2)

    def is_finished(self):
        """Return True if all possible lines have been made, i.e when
        all boxes are filled
        """
        return None not in flatten(self._boxes)

    def winners(self):
        """Return a list of all players who filled in the most boxes

        If multiple players filled in the same number of boxes, return all of those.
        If all boxes haven't been filled yet, the list is empty.
        """
        if not self.is_finished():
            return []

        score = Counter(flatten(self._boxes))
        return [p[0] for p in next(itertools.groupby(score.most_common(), lambda p: p[1]))[1]]

    def winner(self):
        """Return the winner of the game.

        This only returns one player. If there are multiple winners,
        or there's no winner yet, return None.
        """
        it = iter(self.winners())
        winner = next(it, None)
        if winner is None:
            return winner

        if next(it, None) is not None:
            return None

        return winner

    def scoreboard(self):
        c = Counter(b for b in flatten(self._boxes) if b is not None)
        return [(t, c[t]) for t in range(2)]

    @property
    def turn(self):
        return self._turn

    @property
    def width(self):
        return len(self._horizontal[0])

    @property
    def height(self):
        return len(self._vertical)



# Board that uses PIL for image. If you just want to copy the board, Ignore this.
import asyncio
import io

from PIL import Image, ImageDraw, ImageFont

LINE_LENGTH = 120
LINE_WIDTH = 10
LINE_RADIUS = LINE_WIDTH // 2
DOT_SIZE = 20
DOT_RADIUS = DOT_SIZE // 2
LINE_COLOURS = [(255, 0, 0, 255), (0, 0, 255, 255)]


try:
    _XY_FONT = ImageFont.truetype("Arial.ttf", 20)
except Exception:
    _XY_FONT = None

MARGIN_SIZE = 60
TEXT_OFFSET = MARGIN_SIZE / 3


class ImageBoard(Board):
    def _grid_image(self):
        w, h = self.width, self.height
        base = LINE_LENGTH + DOT_SIZE
        size = (base * w + DOT_SIZE, base * h + DOT_SIZE)
        grid_image = Image.new('RGBA', size)
        draw = ImageDraw.Draw(grid_image)

        def draw_from(list_, x_offset, y_offset, handle):
            for y, row in enumerate(list_):
                start_y = base * y + y_offset

                for x, space in enumerate(row):
                    if space is None:
                        continue

                    start_x = base * x + x_offset
                    colour = LINE_COLOURS[space]
                    handle(start_x, start_y, colour)

        def draw_line(index):
            def draw_line(x, y, colour):
                coords = [x, y] * 2
                coords[2 + index] += LINE_LENGTH
                draw.line(coords, fill=colour, width=LINE_WIDTH)
            return draw_line

        draw_from(self._horizontal, DOT_SIZE, DOT_RADIUS, draw_line(0))
        draw_from(self._vertical, DOT_RADIUS, DOT_SIZE, draw_line(1))

        def draw_box(x, y, colour):
            end_x = x + LINE_LENGTH + DOT_RADIUS
            end_y = y + LINE_LENGTH + DOT_RADIUS
            colour = (*(round(c * 0.8) for c in colour[:3]), 255, )
            draw.rectangle([x, y, end_x, end_y], fill=colour)

        draw_from(self._boxes, DOT_RADIUS + LINE_RADIUS, DOT_RADIUS + LINE_RADIUS, draw_box)

        for x, y in itertools.product(range(self.width + 1), range(self.height + 1)):
            ex, ey = base * x, base * y
            draw.ellipse([ex, ey, ex + DOT_SIZE, ey + DOT_SIZE], (0, 0, 0, 255))

        return grid_image

    def _image(self):
        # For performance reasons it might be better to lump this with
        # the grid... For maintainability reasons, I'm separating the
        # grid with the text.
        w, h = self.width, self.height
        base = LINE_LENGTH + DOT_SIZE

        grid = self._grid_image()
        grid_w, grid_h = grid.size
        size = grid_w + MARGIN_SIZE * 2, grid_h + MARGIN_SIZE * 2

        image = Image.new('RGBA', size, (245, 245, 245, 255))
        image.paste(grid, (MARGIN_SIZE, MARGIN_SIZE), mask=grid)
        text_draw = ImageDraw.Draw(image)

        # A-H
        for i, char in enumerate(string.ascii_uppercase[:w + 1]):
            w, h = text_draw.textsize(char, font=_XY_FONT)
            xy = (i * base + MARGIN_SIZE + w // 4, TEXT_OFFSET)
            text_draw.text(xy, char, fill=0, font=_XY_FONT)
        # 1-8
        for i in range(h):
            char = str(i + 1)
            w, h = text_draw.textsize(char, font=_XY_FONT)
            xy = (TEXT_OFFSET, i * base + MARGIN_SIZE)
            text_draw.text(xy, char, fill=0, font=_XY_FONT)

        return image

    def _image_file(self):
        image = self._image()
        f = io.BytesIO()
        image.save(f, 'png')
        f.seek(0)
        return f

    def image(self, *, async_=False, loop=None):
        if not async_:
            return self._image()
        loop = loop or asyncio.get_event_loop()
        return loop.run_in_executor(None, self._image)

    def image_file(self, *, async_=False, loop=None):
        if not async_:
            return self._image_file()
        loop = loop or asyncio.get_event_loop()
        return loop.run_in_executor(None, self._image_file)


# Below is the game logic. If you just want to copy the board, Ignore this.

import contextlib
import random
import re

import discord

from .bases import Status, TwoPlayerGameCog
from ..utils.context_managers import temp_message

_EMOJIS = ['\U0001f534', '\U0001f535']

_VALID_MOVE_REGEX = re.compile(r'^([a-h][1-8]\s?)+', re.IGNORECASE)
_MESSAGES = {
    Status.PLAYING: 'Dots and Boxes',
    Status.END: '{user} wins!',
    Status.QUIT: '{user} forefited...',
    Status.TIMEOUT: '{user} ran out of time...',
}

class DotsAndBoxesSession:
    def __init__(self, ctx, opponent):
        self._ctx = ctx
        self._players = random.sample((ctx.author, opponent), 2)

        self._status = Status.PLAYING
        self._display = (discord.Embed(colour=ctx.bot.colour)
                         .set_image(url='attachment://dots-and-boxes.png')
                         )

        self._board = ImageBoard(3, 3)
        self._image = None

    def _check(self, message):
        if not (message.channel == self._ctx.channel and message.author == self.current):
            return False

        if message.content.lower() in {'stop', 'quit'}:
            self._status = Status.QUIT
            return True

        lowered = message.content.lower()
        if not _VALID_MOVE_REGEX.match(lowered):
            return False

        try:
            self._board.move(''.join(lowered.split()))
        except ValueError:
            return False
        else:
            return True

    async def _update_display(self):
        if self._status is Status.END:
            winner = self._board.winner()
            user = None if winner is None else self._players[winner]
        else:
            user = self.current

        if self._status is Status.PLAYING:
            your_turn = f'{_EMOJIS[self._board.turn]} Your turn, {user}\n'
        else:
            your_turn = ''

        if user:
            header = _MESSAGES[self._status].format(user=user)
        else:
            header = "It's a tie!"

        self._display.set_author(name=header)
        self._display.description = your_turn + ' | '.join(
            f'{_EMOJIS[turn]} {score}' for turn, score in self._board.scoreboard()
        )
        file = await self._board.image_file(async_=True)
        self._image = discord.File(file, 'dots-and-boxes.png')

    async def _loop(self):
        wait_for = self._ctx.bot.wait_for
        # needed cuz we're looking this up a few times
        resigned = Status.QUIT

        while not self._board.is_finished():
            await self._update_display()
            async with temp_message(self._ctx, embed=self._display, file=self._image):
                try:
                    user_message = await wait_for('message', timeout=120, check=self._check)
                except asyncio.TimeoutError:
                    self._status = Status.TIMEOUT
                    return

                with contextlib.suppress(Exception):
                    await user_message.delete()

                if self._status is resigned:
                    return

        self._status = Status.END

    async def run(self):
        try:
            return await self._loop()
        finally:
            await self._update_display()
            await self._ctx.send(embed=self._display, file=self._image)

    @property
    def current(self):
        return self._players[self._board.turn]

class DotsAndBoxes(TwoPlayerGameCog, game_cls=DotsAndBoxesSession, cmd='dots-boxes'):
    async def _end_game(self, ctx, inst, result):
        pass

def setup(bot):
    bot.add_cog(DotsAndBoxes(bot))
 