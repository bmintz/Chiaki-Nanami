import asyncio
import asyncqlio
import contextlib
import discord
import enum
import itertools
import random
import textwrap
import time

from datetime import datetime
from discord.ext import commands
from string import ascii_lowercase, ascii_uppercase

from .manager import SessionManager

from ..tables.base import TableBase
from ..utils.converter import ranged
from ..utils.misc import emoji_url, nice_time, REGIONAL_INDICATORS
from ..utils.paginator import BaseReactionPaginator, EmbedFieldPages, page
from ..utils.time import duration_units

from core.cog import Cog


class MinesweeperException(Exception):
    pass


class HitMine(MinesweeperException):
    def __init__(self, x, y):
        self.point = x, y
        super().__init__(f'hit a mine on {x + 1} {y + 1}')



_Table = asyncqlio.table_base()

class MinesweeperGame(TableBase, table_name='minesweeper_games'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)

    level = asyncqlio.Column(asyncqlio.SmallInt)
    won = asyncqlio.Column(asyncqlio.Boolean)

    guild_id = asyncqlio.Column(asyncqlio.BigInt)
    user_id = asyncqlio.Column(asyncqlio.BigInt)
    played_at = asyncqlio.Column(asyncqlio.Timestamp)

    time = asyncqlio.Column(asyncqlio.Real)
    minesweeper_time_idx = asyncqlio.Index(time)


class MinesweeperLeaderboard(EmbedFieldPages):
    def __init__(self, *args, level, **kwargs):
        super().__init__(*args, **kwargs)
        self.per_page = 5
        self._header = f'Fastest times for {level}'

    def _create_embed(self, idx, page):
        embed = super()._create_embed(idx, page)
        return embed.set_author(name=self._header, icon_url=emoji_url('\N{COLLISION SYMBOL}'))

    numbered = None


class Level(enum.Enum):
    beginner = easy = enum.auto()
    intermediate = medium = enum.auto()
    expert = hard = enum.auto()
    custom = enum.auto()

    def __str__(self):
        return self.name.title()

    @classmethod
    async def convert(cls, ctx, arg):
        lowered = arg.lower()
        try:
            return cls[lowered]
        except KeyError:
            difficulties = '\n'.join(str(m).lower() for m in cls)
            raise commands.BadArgument(f'"{arg}"" is not a valid level. Valid difficulties:\n{difficulties}') from None

class FlagType(enum.Enum):
    default = None
    f = 'flag'
    flag = 'flag'
    u = 'unsure'
    unsure = 'unsure'


class Tile(enum.Enum):
    blank = '\N{WHITE LARGE SQUARE}'
    flag = '\N{TRIANGULAR FLAG ON POST}'
    mine = '\N{EIGHT POINTED BLACK STAR}'
    shown = '\N{BLACK LARGE SQUARE}'
    unsure = '\N{BLACK QUESTION MARK ORNAMENT}'
    boom = '\N{COLLISION SYMBOL}'

    def __str__(self):
        return self.value

    @staticmethod
    def numbered(number):
        return f'{number}\U000020e3'


SURROUNDING = ((-1, -1), (-1,  0), (-1,  1),
               (0 , -1),           (0 ,  1),
               (1 , -1), (1 ,  0), (1 ,  1))


class Board:
    def __init__(self, width, height, mines):
        if mines >= width * height:
            raise ValueError(f'Too many mines (expected max {width * height}, got {mines})')
        if mines <= 0:
            raise ValueError("A least one mine is required")

        self._mine_count = mines

        self._board = [[Tile.blank] * width for _ in range(height)]
        self.visible = set()
        self.flags = set()
        self.unsures = set()
        self.mines = set()

    def __contains__(self, xy):
        return 0 <= xy[0] < self.width and 0 <= xy[1] < self.height

    def __repr__(self):
        return f'{type(self).__name__}({self.width}, {self.height}, {len(self.mines)})'

    def __str__(self):
        return '\n'.join([f"{char} {' '.join(map(str, cells))}"
                          for char, cells in zip(REGIONAL_INDICATORS, self._board)])

    def _place_mines_from(self, x, y):
        surrounding = set(self._get_neighbours(x, y))
        click_area = surrounding | {(x, y)}

        possible_coords = itertools.product(range(self.width), range(self.height))
        coords = [p for p in possible_coords if p not in click_area]

        self.mines = set(random.sample(coords, k=min(self._mine_count, len(coords))))
        self.mines.update(random.sample(surrounding, self._mine_count - len(self.mines)))

        # All mines should be exhausted, unless we somehow made a malformed board.
        assert len(self.mines) == self._mine_count, f"only {len(self.mines)} mines were placed"

    def is_mine(self, x, y):
        return (x, y) in self.mines

    def is_flag(self, x, y):
        return (x, y) in self.flags

    def is_visible(self, x, y):
        return (x, y) in self.visible

    def is_unsure(self, x, y):
        return (x, y) in self.unsures

    def _get_neighbours(self, x, y):
        pairs = ((x + surr_x, y + surr_y) for (surr_x, surr_y) in SURROUNDING)
        return (p for p in pairs if p in self)

    def show(self, x, y):
        if not self.mines:
            self._place_mines_from(x, y)

        if self.is_visible(x, y):
            return

        self.visible.add((x, y))
        if self.is_mine(x, y) and not self.is_flag(x, y):
            raise HitMine(x, y)

        surrounding = sum(self.is_mine(nx, ny) for nx, ny in self._get_neighbours(x, y))
        if not surrounding:
            self._board[y][x] = Tile.shown
            for nx, ny in self._get_neighbours(x, y):
                self.show(nx, ny)
        else:
            self._board[y][x] = Tile.numbered(surrounding)

    def _modify_board(self, x, y, attr):
        if self.is_visible(x, y):
            return

        tup = x, y
        was_thing = getattr(self, f'is_{attr}')(x, y)
        for thing in ('flags', 'unsures'):
            getattr(self, thing).discard(tup)

        if was_thing:
            self._board[y][x] = Tile.blank
        else:
            getattr(self, f'{attr}s').add(tup)
            self._board[y][x] = getattr(Tile, attr)

    def flag(self, x, y):
        self._modify_board(x, y, 'flag')

    def unsure(self, x, y):
        self._modify_board(x, y, 'unsure')

    def reveal_mines(self, success=False):
        tile = Tile.flag if success else Tile.boom
        for mx, my in self.mines:
            self._board[my][mx] = tile

    def hide_mines(self):
        for mx, my in self.mines:
            self._board[my][mx] = Tile.blank

    def explode(self, x, y):
        if not self.is_visible(x, y):
            return
        self._board[y][x] = Tile.boom

    def is_solved(self):
        return len(self.visible) + len(self.mines) == self.width * self.height

    @property
    def width(self):
        return len(self._board[0])

    @property
    def height(self):
        return len(self._board)

    @property
    def mine_count(self):
        return len(self.mines) or self._mine_count

    @property
    def mines_marked(self):
        return len(self.flags)

    @property
    def remaining_flags(self):
        return self.mine_count - self.mines_marked

    @property
    def remaining_mines(self):
        return len(self.mines - self.flags)

    @classmethod
    def beginner(cls):
        """Returns a beginner minesweeper board"""
        return cls(9, 9, 10)

    @classmethod
    def intermediate(cls):
        """Returns a intermediate minesweeper board"""
        return cls(12, 12, 20)

    @classmethod
    def expert(cls):
        """Returns an expert minesweeper board"""
        return cls(13, 13, 40)


class _MinesweeperHelp(BaseReactionPaginator):
    def __init__(self, game):
        super().__init__(game.ctx)
        self.game = game

    @property
    def board(self):
        return self.game.board

    @page('\N{INFORMATION SOURCE}')
    def default(self):
        """How to navigate this help page (this page)"""
        desc = 'Basically the goal is to reveal all of the board and NOT get hit with a mine!'
        instructions = 'To navigate through this help page, click one of the reactions below'

        return (discord.Embed(colour=self.colour, description=desc)
                .set_author(name='Welcome to Minesweeper!')
                .add_field(name=instructions, value=self.reaction_help)
                )

    @page('\N{VIDEO GAME}')
    def controls(self):
        """Controls for playing Minesweeper"""
        text = textwrap.dedent(f'''
        Basically the goal is to reveal all of the board and NOT get hit with a mine!

        To make a move, send a message in this format:
        ```
        <column> <row> [f|flag|u|unsure]
        ```
        Column must be from **A-{ascii_lowercase[self.board.width - 1].upper()}**
        And row must be from **A-{ascii_lowercase[self.board.height - 1].upper()}**
        Typing `f` or `flag` will mark the tile with a flag.
        Typing `u` or `unsure` will mark the tile as unsure.
        Typing nothing, well you know what it will do.

        You **do not** need to include the `<>` or `[]`.

        Note that you can only input it if you're in this actual game.
        (ie typing anything in this screen won't do anything.)
        \u200b
        ''')
        return (discord.Embed(colour=self.colour, description=text)
                .set_author(name='How to play Minesweeper')
                .add_field(name='Reactions you can click on in the game', value=self.game._game_screen.reaction_help)
                )

    @staticmethod
    def _possible_spaces():
        number = random.randint(1, 9)
        return textwrap.dedent(f'''
        {Tile.shown} - Empty tile, reveals other empty or numbered tiles near it

        {Tile.numbered(number)} - Displays the number of mines surrounding it.
        This one shows that they are {number} mines around it.

        {Tile.boom} - BOOM! Selecting a mine makes it explode, causing all other mines to explode
        and thus ending the game. Avoid mines at any costs!
        \u200b
        ''')

    @page('\N{COLLISION SYMBOL}')
    def possible_spaces(self):
        """Things you might hit when you select a tile"""
        description = (
            'When you select a tile, chances are you will hit one of these 3 things.\n'
            + self._possible_spaces()
        )

        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name='Tiles')
                )

    @page('\N{BLACK SQUARE FOR STOP}')
    async def stop(self):
        """Closes this help page"""
        await self.game.edit_board(self.colour, header=self.game._default_header)
        return super().stop()


class _MinesweeperDisplay(BaseReactionPaginator):
    def __init__(self, game):
        super().__init__(game.ctx)
        self.game = game
        self.state = None
        self._help_future = self.context.bot.loop.create_future()
        self._help_future.set_result(None)  # We just need an already done future.

    @property
    def board(self):
        return self.game.board

    def _board_repr(self):
        top_row = ' '.join(REGIONAL_INDICATORS[:self.board.width])
        # Discord strips any leading and trailing spaces.
        # By putting a zero-width space we bypass that
        return f'\N{BLACK LARGE SQUARE} {top_row}\n{self.board}'

    def is_on_help(self):
        return not self._help_future.done()

    def default(self):
        board = self.board
        return (discord.Embed(colour=self.colour, description=self._board_repr())
                .set_author(name=self.game._default_header)
                .add_field(name='Player', value=self.context.author)
                .add_field(name='Mines Marked', value=f'{board.mines_marked} / {board.mine_count}')
                .add_field(name='Flags Remaining', value=board.remaining_flags)
                .add_field(name='\u200b', value='Stuck? Click the \N{INFORMATION SOURCE} reaction for some help.')
                )

    @page('\N{INFORMATION SOURCE}')
    async def help_page(self):
        """Gives you a help page (the page you're currently looking at)"""
        if self._help_future.done():
            await self.game.edit_board(0x90A4AE, header='Currently on the help page...')
            self._help_future = asyncio.ensure_future(_MinesweeperHelp(self.game).interact())

    @page('\N{BLACK SQUARE FOR STOP}')
    def stop(self):
        """Stops the game"""
        self.game.stop()
        # In case the user has the help page open when canceling it
        # (this shouldn't technically happen but this is here just in case.)
        if not self._help_future.done():
            self._help_future.cancel()

        return super().stop()

    async def edit(self, embed):
        await self._message.edit(embed=embed)


class MinesweeperSession:
    def __init__(self, ctx, board):
        self.board = board
        self.ctx = ctx
        self._interaction = None
        self._runner = None
        self._game_screen = _MinesweeperDisplay(self)
        self._default_header = f'Minesweeper - {board.width} x {board.height}'

    def check_message(self, message):
        return (not self._game_screen.is_on_help()
                and message.channel == self.ctx.channel
                and message.author == self.ctx.author)

    def parse_message(self, content):
        splitted = content.lower().split(None, 3)[:3]
        chars = len(splitted)

        if chars == 2:
            flag = FlagType.default
        elif chars == 3:
            flag = getattr(FlagType, splitted[2].lower(), FlagType.default)
        else:  # We need at least the x, y coordinates...
            return None

        try:
            x, y = map(ascii_lowercase.index, splitted[:2])
        except ValueError:
            return None
        else:
            if (x, y) not in self.board:
                return None
            return x, y, flag

    async def edit_board(self, new_colour=None, *, header=None):
        embed = self._game_screen.default()

        header = header or self._default_header
        embed.set_author(name=header)

        if new_colour is not None:
            embed.colour = new_colour

        await self._game_screen.edit(embed=embed)

    async def _loop(self):
        start = time.perf_counter()
        while True:
            colour = header = None
            try:
                message = await self.ctx.bot.wait_for('message', timeout=120, check=self.check_message)
            except asyncio.TimeoutError:
                await self.ctx.send(f'{self.ctx.author.mention} You took too long!')
                await self.edit_board(0, header='Took too long...')
                return None

            parsed = self.parse_message(message.content)
            if parsed is None:      # garbage input, ignore.
                continue
            x, y, thing = parsed
            with contextlib.suppress(discord.NotFound):
                await message.delete()

            try:
                if thing.value:
                    getattr(self.board, thing.value)(x, y)
                else:
                    self.board.show(x, y)
            except HitMine:
                self.board.explode(x, y)
                await self.edit_board(0xFFFF00, header='BOOM!')
                await asyncio.sleep(random.uniform(0.5, 1))
                self.board.reveal_mines()
                colour, header = 0xFF0000, 'Game Over!'
                raise
            except Exception as e:
                await self.ctx.send(f'An error happened.\n```\y\n{type(e).__name__}: {e}```')
                raise
            else:
                if self.board.is_solved():
                    colour, header = 0x00FF00, "A winner is you!"
                    self.board.reveal_mines(success=True)
                    return time.perf_counter() - start
            finally:
                await self.edit_board(colour, header=header)

    async def run_loop(self):
        try:
            return await self._loop()
        except asyncio.CancelledError:
            await self.edit_board(0, header='Minesweeper stopped.')
            raise

    async def run(self):
        self._interaction = asyncio.ensure_future(self._game_screen.interact(timeout=None, delete_after=False))
        self._runner = asyncio.ensure_future(self.run_loop())
        # await self._game_screen.wait_until_ready()
        try:
            return await self._runner
        finally:
            # For some reason having all these games hanging around causes lag.
            # Until I properly make a delete_after on the paginator I'll have to
            # settle with this hack.
            async def task():
                await asyncio.sleep(30)
                with contextlib.suppress(discord.HTTPException):
                    await self._game_screen._message.delete()

            self.ctx.bot.loop.create_task(task())
            self._interaction.cancel()

    def stop(self):
        for task in (self._runner, self._interaction):
            with contextlib.suppress(BaseException):
                task.cancel()


class Minesweeper(Cog):
    def __init__(self, bot):
        self.bot = bot
        self.manager_bucket = {level: SessionManager() for level in Level}

    async def _get_record_text(self, ctx, level, time):
        # Check if it's the world record
        query = """SELECT MIN(time) as "world_record"
                   FROM minesweeper_games
                   WHERE won AND level = {level};
                """
        world_record = (await ctx.session.fetch(query, {'level': level.value}))['world_record']
        print(world_record, time)

        if world_record is None or time < world_record:
            return "You've broken the world record. Congratulations!!"

        # Check if it's a personal best
        query = """SELECT MIN(time) as "pb"
                   FROM minesweeper_games
                   WHERE won AND level = {level} AND user_id = {user_id};
                """
        params = {'level': level.value, 'user_id': ctx.author.id}
        personal_best = (await ctx.session.fetch(query, params))['pb']

        if personal_best is None or time < personal_best:
            return "This is a new personal best!"

        return ''

    async def _do_minesweeper(self, ctx, level, board, *, record_time=True):
        manager = self.manager_bucket[level]
        session = manager.get_session(ctx.author.id)
        if session is not None:
            return await ctx.send(f'You already have a {level} Minesweeper game '
                                  f'in {session.ctx.channel} from {session.ctx.guild}')

        await ctx.send(f'Starting a {level} minesweeper game...')
        with manager.temp_session(ctx.author.id, MinesweeperSession(ctx, board)) as inst:
            # Wrapping this in try-except because these exceptions will be
            # raised in the game itself. We don't want the global error handlers
            # taking care of these because they're notr really error but
            # more along the lines of flow control.
            won = False
            try:
                time = await inst.run()
            except HitMine as error:
                x, y = error.point
                await ctx.send(f'You hit a mine on {ascii_uppercase[x]} {ascii_uppercase[y]}... ;-;')
                time = 0
            except asyncio.CancelledError:
                return await ctx.send(f'Ok, cya later...')
            else:
                won = True

        if won and time:
            rounded = round(time, 2)
            text = f'You beat Minesweeper on {level} in {duration_units(rounded)}.'

            extra_text = ''
            # Check if the player broke the world record.
            if record_time:
                extra_text = await self._get_record_text(ctx, level, time)

            description = f'{text}\n{extra_text}'

            win_embed = (discord.Embed(colour=0x00FF00, timestamp=datetime.utcnow(), description=description)
                         .set_author(name='A winner is you!')
                         .set_thumbnail(url=ctx.author.avatar_url)
                         )

            await ctx.send(embed=win_embed)

        if record_time:
            await ctx.session.add((MinesweeperGame(
                level=level.value,
                won=won,

                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                played_at=ctx.message.created_at,
                time=time or 0,
            )))

    @commands.group(aliases=['msw'], invoke_without_command=True)
    async def minesweeper(self, ctx, level: Level=Level.beginner):
        """Starts a game of Minesweeper"""
        board = getattr(Board, str(level).lower())()
        """Starts a game of Minesweeper."""
        await self._do_minesweeper(ctx, level, board)

    @minesweeper.command(name='custom')
    async def minesweeper_custom(self, ctx, width: ranged(3, 20), height: ranged(3, 20), mines: int):
        """Starts a custom minesweeper game."""
        if not 9 <= width * height <= 170:
            return await ctx.send("Can't have a board of that size due to emoji bugs sorry ;-;")

        try:
            board = Board(width, height, mines)
        except ValueError as e:
            await ctx.send(e)
        else:
            await self._do_minesweeper(ctx, Level.custom, board, record_time=False)

    @minesweeper.command(name='stop')
    async def minesweeper_stop(self, ctx, level: Level):
        """Stops a currently running minesweeper game.

        Ideally, you should not have to call this, because the game already
        has a stop button in place.
        """
        manager = self.manager_bucket[level]
        session = manager.get_session(ctx.author.id)
        if session is None:
            return await ctx.send("You don't have a {level} minesweeper running...")

        session.stop()

    @minesweeper.command(name='leaderboard', aliases=['lb'])
    async def minesweeper_leaderboard(self, ctx, level: Level=Level.beginner):
        """Returns the top 25 fastest times for a difficulty level of minesweeper. Defaults to Beginner."""
        query = (ctx.session.select.from_(MinesweeperGame)
                            .where((MinesweeperGame.won == True)
                                   & (MinesweeperGame.level == level.value))
                            .order_by(MinesweeperGame.time)
                            .limit(25)
                 )

        entries = []
        add_entry = entries.append
        async for row in await query.all():
            name = duration_units(row.time)
            server = ctx.bot.get_guild(row.guild_id) or f'<Unknown server, ID={row.guild_id}>'
            value = f'Achieved by: <@{row.user_id}> in {server} on `{nice_time(row.played_at)}`'

            add_entry((name, value))

        pages = MinesweeperLeaderboard(ctx, entries, level=level, inline=False)
        await pages.interact()


def setup(bot):
    bot.add_cog(Minesweeper(bot))
