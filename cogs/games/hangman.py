import asyncio
import contextlib
import discord
import functools
import glob
import operator
import os
import random
import string

from collections import namedtuple
from discord.ext import commands

from .manager import SessionManager

from ..utils.formats import escape_markdown, truncate
from ..utils.misc import base_filename, group_strings
from ..utils.paginator import ListPaginator

from core.cog import Cog


# I hate string concatentation
# And I hate constantly building the same string
# So this is done to avoid both of those.

_template = '''
   11111
  0    2
  0    3
  0   546
  0    4
  0   7 8
 _0_
|   |______
|          |
|__________|
'''

_symbols = string.octdigits + '8'
_blanks = dict.fromkeys(map(ord, _symbols), ' ')

hangman_drawings = []
for fake, real in zip(_symbols, '|_|o|/\\/\\'):
    hangman_drawings.append(_template.translate(_blanks))
    _template = _template.replace(fake, real)
hangman_drawings.append(_template)
del _template

GameResult = namedtuple('GameResult', 'success message')
GameResult.__bool__ = operator.attrgetter('success')

INSTRUCTIONS = ('Type a letter to guess a letter, \n'
                'or prefix your phrase with `*` to guess the phrase.\n'
                '(eg `*the quick brown fox`)')
class HangmanSession:
    def __init__(self, ctx, word):
        self.ctx = ctx
        self.word = word
        self._lowered_word = self.word.lower()
        self.blanks = ['_' if letter in string.ascii_letters else letter
                       for letter in word]
        self._guesses = []
        self.fails = 0
        self._runner = None

        self._game_screen = (discord.Embed(colour=0x00FF00)
                             .set_author(name='Hangman Game Started!')
                             .add_field(name='Guesses', value='\u200b')
                             .add_field(name='Average', value=0)
                             .add_field(name='Instructions', value=INSTRUCTIONS, inline=False)
                             )

    def _verify_guess(self, guess):
        lowered = guess.lower()
        if lowered in self._guesses:
            return GameResult(success=None, message=f"{guess} was already guessed!")

        if len(lowered) != 1:      # full word
            if lowered == self._lowered_word:
                return GameResult(success=True, message="You guessed it!")
            return GameResult(success=False, message=f"{guess} is not the word :(")

        if lowered in self._lowered_word:
            return GameResult(success=True, message=f"{guess} is in the word :D")
        return GameResult(success=False, message=f"{guess} is not in the word :(")

    def _check_message(self, message):
        if message.channel != self.ctx.channel or message.author.bot:
            return False

        content = message.content
        return len(content) == 1 or content.startswith('*')

    def edit_screen(self):
        guess = '\n'.join(group_strings(', '.join(self.guesses), 35))
        self._game_screen.description = f'```{" ".join(self.blanks)}```\n```{hangman_drawings[self.fails]}```'
        self._game_screen.set_field_at(0, name='Guesses', value=truncate(guess, 1024, '...') or '\u200b')
        self._game_screen.set_field_at(1, name='Average', value=f'{self.average() * 100 :.2f}%')

    async def _loop(self, message):
        while True:
            guess = await self.ctx.bot.wait_for('message', check=self._check_message)
            content = guess.content.lower()
            content = content[len(content) > 1:]

            ok, result = self._verify_guess(content)
            if ok:
                self._game_screen.colour = 0x00FF00
                self.blanks[:] = (c if c.lower() in content else v for c, v in zip(self.word, self.blanks))
            else:
                self._game_screen.colour = 0xFF0000
                self.fails += ok is not None
            if ok is not None:
                self._guesses.append(content)

            self.edit_screen()
            with contextlib.suppress(discord.HTTPException):
                await guess.delete()
            try:
                if self.is_completed():
                    self._game_screen.set_author(name='Hangman Completed!')
                    break
                elif self.is_dead():
                    self._game_screen.set_author(name='GAME OVER')
                    break
            finally:
                await message.edit(content=f'{guess.author.mention}, {result}', embed=self._game_screen)

    async def run_loop(self):
        self.edit_screen()
        message = await self.ctx.send(embed=self._game_screen)
        self._game_screen.set_author(name='Hangman Game')

        try:
            await self._loop(message)
        except asyncio.CancelledError:
            self._game_screen.set_author(name='Hangman stopped...')
            self._game_screen.colour = 0
            await message.edit(embed=self._game_screen)
            raise
        else:
            message = f'The answer was **{escape_markdown(self.word)}**.'
            return GameResult(success=not self.is_dead(), message=message)

    async def run(self):
        self._runner = asyncio.ensure_future(self.run_loop())
        return await self._runner

    def stop(self):
        self._runner.cancel()

    def average(self):
        return 1 - (self.fails / len(self.guesses)) if self.fails else 0

    def is_completed(self):
        return '_' not in self.blanks

    def is_dead(self):
        return self.fails >= len(hangman_drawings) - 1

    @property
    def guesses(self):
        return ['`{0}`'.format(g.replace("`", r"\`")) for g in self._guesses]

def _load_hangman(filename):
    with open(filename) as f:
        return [line for line in map(str.strip, f) if line and line[0] != '#' and len(line) > 4]

class Hangman(Cog):
    """So you don't have to hang people in real life."""
    FILE_PATH = os.path.join('.', 'data', 'words')

    def __init__(self, bot):
        self.bot = bot
        self.manager = SessionManager()
        self.bot.loop.create_task(self._load_categories())
        self.default_categories = {}

    def __unload(self):
        self.manager.cancel_all()

    async def _load_categories(self):
        load_async = functools.partial(self.bot.loop.run_in_executor, None, _load_hangman)
        files = glob.glob(f'{self.FILE_PATH}/*.txt')
        load_tasks = (load_async(name) for name in files)
        file_names = (base_filename(name) for name in files)

        categories = self.default_categories
        categories.update(zip(file_names, await asyncio.gather(*load_tasks)))

        # Delete any empty categories
        for k, v in list(categories.items()):
            if not v:
                del categories[k]

        print('everything is ok now')

    async def _get_category(self, ctx, category):
        lowered = category.lower()
        c = self.default_categories.get(lowered)
        if not c:
            raise commands.BadArgument(f"Category {category} doesn't exist... :(")
        return c

    @commands.group(invoke_without_command=True)
    @commands.bot_has_permissions(embed_links=True)
    async def hangman(self, ctx, category):
        """Starts a game of hangman.

        To see all the categories you can choose, 
        type `{prefix}hangman categories`
        """
        if self.manager.session_exists(ctx.channel):
            return await ctx.send("A hangman game is already running in this channel...")

        words = await self._get_category(ctx, category)
        word = random.choice(words)

        await ctx.release()
        with self.manager.temp_session(ctx.channel, HangmanSession(ctx, word)) as inst:
            success, message = await inst.run()
            if success is None:
                return

            game_over_message = 'You did it!' if success else 'Noooo you lost. \N{CRYING FACE}'
            await ctx.send(f'{game_over_message} {message}')

    @hangman.command(name='stop')
    async def hangman_stop(self, ctx):
        """Stops a running hangman game."""
        instance = self.manager.get_session(ctx.channel)
        if instance is None:
            return await ctx.send('There is no hangman running right now...')

        instance.stop()

    @hangman.command(name='categories')
    async def hangman_categories(self, ctx):
        """Stops a running hangman game."""
        categories = self.default_categories
        embeds = ListPaginator(ctx, sorted(categories), title=f'List of Categories for {ctx.guild}',
                               colour=discord.Colour.blurple())
        await embeds.interact()

def setup(bot):
    bot.add_cog(Hangman(bot))
