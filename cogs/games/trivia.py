import asyncio
import collections
import contextlib
import glob
import io
import itertools
import inspect
import json
import logging
import os
import random
import time
from difflib import SequenceMatcher
from html import unescape

import aiohttp
import discord
from discord.ext import commands
from more_itertools import always_iterable
from PIL import Image

from ..utils import cache
from ..utils.deprecated import DeprecatedCommand
from ..utils.formats import pluralize
from ..utils.misc import emoji_url


class StopTrivia(Exception):
    pass


POINTS_TO_WIN = 10
TRIVIA_ANSWER_TIMEOUT = 60
TIMEOUT_ICON = emoji_url('\N{ALARM CLOCK}')

logger = logging.getLogger(__name__)


class BaseTriviaSession:
    def __init__(self, ctx):
        self._ctx = ctx
        self._score_board = collections.Counter()
        self._answer_waiter = asyncio.Event()
        self._stop_trivia = asyncio.Event()
        self._current_question = None

    # These methods must be overridden by subclasses

    async def _get_question(self):
        raise NotImplementedError

    def _check(self, message):
        raise NotImplementedError

    async def _show_question(self, number):
        raise NotImplementedError

    # End of the abstract methods

    # These methods can be overriden by subclasses.

    def _answer_embed(self, user):
        score = self._score_board[user.id]
        action = 'wins the game' if score >= POINTS_TO_WIN else 'got it'
        description = f'The answer was **{self._current_question.answer}**.'

        return (discord.Embed(colour=0x00FF00, description=description)
                .set_author(name=f'{user} {action}!')
                .set_thumbnail(url=user.avatar_url)
                .set_footer(text=f'{user} now has {score} points.')
                )

    def _timeout_embed(self):
        answer = self._current_question.answer
        return (discord.Embed(description=f'The answer was **{answer}**', colour=0xFF0000)
                .set_author(name='Times up!', icon_url=TIMEOUT_ICON)
                .set_footer(text='No one got any points :(')
                )

    async def _show_answer(self, user=None, *, correct=True):
        embed = self._answer_embed(user) if correct else self._timeout_embed()
        await self._ctx.send(embed=embed)

    # End.

    async def _loop(self):
        wait_for = self._ctx.bot.wait_for

        for q in itertools.count(1):
            self._current_question = await self._get_question()
            await self._show_question(q)

            try:
                message = await wait_for('message', timeout=20, check=self._check)
            except asyncio.TimeoutError:
                await self._show_answer(correct=False)
            else:
                user = message.author
                self._score_board[user.id] += 1
                await self._show_answer(user)

                if self._score_board[user.id] >= 10:
                    break
            finally:
                await asyncio.sleep(random.uniform(1.5, 3))

    async def _wait_for_answer(self):
        while True:
            await asyncio.wait_for(self._answer_waiter.wait(), timeout=TRIVIA_ANSWER_TIMEOUT)
            self._answer_waiter.clear()

    async def _wait_until_stopped(self):
        await self._stop_trivia.wait()
        raise StopTrivia

    async def run(self):
        tasks = [
            self._loop(),
            self._wait_for_answer(),
            self._wait_until_stopped(),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()

        content = None
        colour = 0

        try:
            done.pop().result()
        except StopTrivia:
            colour = 0xF44336
            content = 'Trivia stopped.'
        except asyncio.TimeoutError:
            colour = 0x607D8B
            content = 'No one is here...'
        finally:
            embed = self.leaderboard_embed()
            if colour:
                embed.colour = colour
            await self._ctx.send(content=content, embed=embed)

    def leaderboard_embed(self):
        if self._score_board:
            description = '\n'.join(
                f'<@!{user_id}> has {pluralize(point=points)}'
                for user_id, points in self.leaderboard)
        else:
            description = 'No one got any points... :('

        return (discord.Embed(colour=0x4CAF50, description=description)
                .set_author(name='Trivia Leaderboard')
                )

    def stop(self):
        self._stop_trivia.set()

    @property
    def leaderboard(self):
        return self._score_board.most_common()

    @property
    def leader(self):
        leaderboard = self.leaderboard
        return leaderboard[0] if leaderboard else None


class OTDBQuestion(collections.namedtuple('_OTDBQ', 'category type question answer incorrect')):
    __slots__ = ()

    @property
    def choices(self):
        a = [self.answer, *self.incorrect]
        return random.sample(a, len(a))

    @classmethod
    def from_data(cls, data):
        return cls(
            category=data['category'],
            type=data['type'],
            question=unescape(data['question']),
            answer=unescape(data['correct_answer']),
            incorrect=tuple(map(unescape, data['incorrect_answers'])),
        )

class DefaultTriviaSession(BaseTriviaSession):
    BASE = 'https://opentdb.com/api.php'
    TOKEN_BASE = 'https://opentdb.com/api_token.php'

    # How many questions to fetch from the API
    AMOUNT = 50

    # How long it takes since the token was last used before it expires
    TOKEN_EXPIRY_TIME = 60 * 60 * 6

    # These are local to the class and are not meant to be used publicly.
    # However they're global as all trivia games will be using the same type
    # of questions.
    _session = aiohttp.ClientSession()

    # The API token used for OTDB. This will ensure that we don't get the
    # same question twice.
    __token = None

    # A lock is needed here because we're messing with class-global state.
    __lock = asyncio.Lock()

    # A cache is required to make sure we have a fallback for when we exhaust all questions.
    __cache = set()

    # This is needed to check if we exhausted all possible questions.
    __exhausted = False

    # This is needed to check if the token has expired. The token gets deleted
    # after 6 hours of inactivity.
    __last_used = time.monotonic()

    def __init__(self, ctx):
        super().__init__(ctx)

        self._choices = None
        self._pending = []
        self._answerers = set()

        if len(self.__cache) >= 500:
            # We should probably pre-fill this because most trivia games
            # don't last very long. So we don't need to make an HTTP request
            # when we don't have to.
            self._pending.extend(random.sample(self.__cache, 15))

    @classmethod
    async def initialize(cls):
        if cls.__token is not None:
            return

        logger.info(f'Creating a new OTDB token at {time.monotonic()}')
        async with cls._session.get(cls.TOKEN_BASE, params=dict(command='request')) as r:
            response = await r.json()

        assert response['response_code'] == 0
        cls.__token = response['token']
        cls.__last_used = time.monotonic()

    @classmethod
    def to_args(cls):
        if not cls.__token:
            return None

        now = time.monotonic()
        if now - cls.__last_used >= cls.TOKEN_EXPIRY_TIME:
            return None

        return cls.__token, cls.__cache, cls.__last_used

    @classmethod
    def refresh(cls, args):
        logger.info('Re-using OTDB token from last reload.')
        cls.__token, cls.__cache, cls.__last_used = args

    async def __get(self):
        if self.__token is None:
            await self.initialize()

        params = dict(amount=self.AMOUNT, token=self.__token)
        async with self._session.get(self.BASE, params=params) as r:
            response = await r.json()
            self.__class__.__last_used = time.monotonic()

        if response['response_code'] == 3:
            # Ensure this is None as initialize checks for None token.
            self.__class__.__token = None
            # The token has expired. We need to regenerate it
            await self.initialize()
            # We don't need the cache because we're going to get dupes anyway.
            self.__cache.clear()
            # recurrrrsion cuz why not
            return await self.__get()

        if response['response_code'] == 4:
            # We've exhausted all possible questions. At this point we have
            # to exclusively use the cache.
            self.__exhausted = True
            self.__token = None  # This token isn't good anymore.
            assert self.__cache, 'cache is empty even though questions have been exhausted'
            return random.sample(self.__cache, self.AMOUNT)

        questions = tuple(map(OTDBQuestion.from_data, response['results']))
        self.__cache.update(questions)
        return questions

    async def _get_question(self):
        # Because we're getting a new question we need to refresh the answerer cache.
        self._answerers.clear()

        if not self._pending:
            if self.__exhausted:
                # We don't need a lock for this because we don't modify
                # the cache.
                questions = random.sample(self.__cache, self.AMOUNT)
            else:
                async with self.__lock:
                    questions = await self.__get()
            self._pending.extend(questions)

        return self._pending.pop()

    def _check(self, message):
        if message.channel != self._ctx.channel:
            return False

        # Prevent other bots from accidentally answering the question
        # This issue has happened numberous times with other bots.
        if message.author.bot:
            return False

        author_id = message.author.id
        # Prevent just simply spamming the answer to win.
        if author_id in self._answerers:
            return False

        if not message.content.isdigit():
            # Do not allow negative numbers because they're not gonna be
            # listed in the answers. We don't wanna confuse users here.
            return False

        number = int(message.content)

        try:
            choice = self._choices[number - 1]
        except IndexError:
            return False

        # We only want people who actually try.
        self._answer_waiter.set()
        self._answerers.add(author_id)
        # TODO: Delete answers if bot has perms?
        return choice == self._current_question.answer

    async def _show_question(self, number):
        question = self._current_question
        # This has to be cached because OTDBQuestion.choices scrambles them each time.
        self._choices = question.choices

        leader = self.leader
        leader_text = (
            f'{self._ctx.bot.get_user(leader[0])} with {leader[1]} points'
            if leader else None
        )

        tf_header = '**True or False**\n' * (question.type == 'boolean')
        question = f'{tf_header}{question.question}'
        possible_answers = '\n'.join(
            itertools.starmap('{0}. {1}'.format, enumerate(self._choices, 1))
        )

        embed = (discord.Embed(description=question, colour=random.randint(0, 0xFFFFFF))
                 .set_author(name=f'Question #{number}')
                 .add_field(name='Choices', value=possible_answers, inline=False)
                 .add_field(name='Leader', value=leader_text, inline=False)
                 .set_footer(text='Questions provided by opentdb.com')
                 )

        await self._ctx.send(embed=embed)


class _FuzzyMatchCheck:
    """Mixin for trivia sessions that rely on typing the answer"""
    @staticmethod
    def _check_answer(message, answer):
        return message.lower() == answer.lower()

    def _check(self, message):
        if message.channel != self._ctx.channel:
            return False

        # Prevent other bots from accidentally answering the question
        # This issue has happened numberous times with other bots.
        if message.author.bot:
            return False

        self._answer_waiter.set()
        return self._check_answer(message.content, self._current_question.answer)


# ------------------ Diep.io --------------------

Question = collections.namedtuple('Question', 'question answer')
TANK_ICON = 'https://vignette.wikia.nocookie.net/diepio/images/f/f2/Tank_Screenshot2.png'

class DiepioTriviaSession(_FuzzyMatchCheck, BaseTriviaSession):
    try:
        with open(os.path.join('.', 'data', 'games', 'trivia', 'diepio.json')) as f:
            data = json.load(f)
    except FileNotFoundError:
        _questions = []
    else:
        logger.info('Successfully loaded diep.io trivia.')
        # Supporting old cruft from when I wanted to do various built-in categories
        _questions = [Question(**d) for d in data['questions']]

    async def _get_question(self):
        return random.choice(self._questions)

    async def _show_question(self, number):
        question = self._current_question.question

        leader = self.leader
        leader_text = (
            f'{self._ctx.bot.get_user(leader[0])} with {leader[1]} points'
            if leader else None
        )

        embed = (discord.Embed(description=question, colour=random.randint(0, 0xFFFFFF))
                 .set_author(name=f'Question #{number}', icon_url=TANK_ICON, url='http://diep.io')
                 .set_footer(text=f'Leader: {leader_text}')
                 )

        await self._ctx.send(embed=embed)

    def _timeout_embed(self):
        answer = next(iter(always_iterable(self._current_question.answer)))
        return (discord.Embed(description=f'The answer was **{answer}**', colour=0xFF0000)
                .set_author(name='Times up!', icon_url=TIMEOUT_ICON)
                .set_footer(text='No one got any points :(')
                )

    def _answer_embed(self, user):
        # Let us pray for the day that we can remove diep.io trivia...
        # always_iterable is a tuple <4.0, but for easy support we should use this.
        answer = next(iter(always_iterable(self._current_question.answer)))
        score = self._score_board[user.id]
        action = 'wins the game' if score >= POINTS_TO_WIN else 'got it'
        description = f'The answer was **{answer}**.'

        return (discord.Embed(colour=0x00FF00, description=description)
                .set_author(name=f'{user} {action}!')
                .set_thumbnail(url=user.avatar_url)
                .set_footer(text=f'{user} now has {score} points.')
                )

    @staticmethod
    def _check_answer(message, answer):
        lowered = message.lower()
        return any(lowered == a.lower() for a in always_iterable(answer))


# ------------------ Pokemon --------------------

POKEMON_PATH = os.path.join('data', 'pokemon')
POKEMON_IMAGE_PATH = os.path.join(POKEMON_PATH, 'images')
POKEMON_NAMES_FILE = os.path.join(POKEMON_PATH, 'names.json')

class PokemonQuestion(collections.namedtuple('PokemonQuestion', 'index, answer image')):
    __slots__ = ()

    @property
    def file(self):
        return os.path.join(POKEMON_PATH, 'images', f'{self.index}.png')

# Image utilities

def _create_silouhette(index):
    with open(os.path.join(POKEMON_IMAGE_PATH, f'{index}.png'), 'rb') as f, \
            Image.open(f) as im:

        image = Image.new(im.mode, im.size)
        image.putdata([
            (30, 30, 30, 255) if a >= 100 else (0, 0, 0, 0)
            for *_, a in im.getdata()
        ])
        return image

def _write_image_to_file(image):
    f = io.BytesIO()
    image.save(f, 'png')
    f.seek(0)
    return discord.File(f, 'pokemon.png')

@cache.cache(maxsize=None)
async def _create_silouhette_async(index):
    run = asyncio.get_event_loop().run_in_executor
    return await run(None, _create_silouhette, index)

async def _get_silouhette(index):
    run = asyncio.get_event_loop().run_in_executor
    image = await _create_silouhette_async(index)
    return await run(None, _write_image_to_file, image)


class PokemonTriviaSession(_FuzzyMatchCheck, BaseTriviaSession):
    try:
        with open(POKEMON_NAMES_FILE) as f:
            _pokemon_names = json.load(f)
    except FileNotFoundError:
        logger.warn(f'{POKEMON_NAMES_FILE} not found. Could not load pokemon names.')
        _pokemon_names = {}
    else:
        logger.info(f'Successfully loaded {POKEMON_NAMES_FILE}.')

    async def _get_question(self):
        file = random.choice(glob.glob(f'{POKEMON_IMAGE_PATH}/*.png'))

        # Note that this assumes that the filename is the pokedex number
        # of the pokemon. The way the pictures are meant to be stored is
        # pokedex_no.png. For example, Bulbasaur's image is meant to be
        # stored as "1.png".
        #
        # This partition is meant for Pokemon that have alternate forms,
        # such as Shaymin or Deoxys. Their images as stored as
        # pokedex_no-some_num.png. This is bad because it can cause KeyErrors
        # since the names.json only has the actual Pokedex number without
        # any other info.
        index = os.path.splitext(os.path.basename(file))[0]
        answer = self._pokemon_names[index.partition('-')[0]]
        image = await _get_silouhette(index)
        return PokemonQuestion(index, answer, image)

    async def _show_question(self, number):
        question = self._current_question

        leader = self.leader
        leader_text = (
            f'{self._ctx.bot.get_user(leader[0])} with {leader[1]} points'
            if leader else None
        )

        embed = (discord.Embed(colour=random.randint(0, 0xFFFFFF))
                 .set_author(name=f"Who's That Pokemon?")
                 .set_image(url='attachment://pokemon.png')
                 .set_footer(text=f'Leader: {leader_text}')
                 )

        await self._ctx.send(embed=embed, file=question.image)

    def _timeout_embed(self):
        return (super()._timeout_embed()
                .set_image(url='attachment://answer.png')
                )

    def _answer_embed(self, user):
        return (super()._answer_embed(user)
                .set_image(url='attachment://answer.png')
                )

    async def _show_answer(self, user=None, *, correct=True):
        embed = self._answer_embed(user) if correct else self._timeout_embed()
        file = discord.File(self._current_question.file, 'answer.png')

        await self._ctx.send(embed=embed, file=file)


class Trivia:
    def __init__(self, bot):
        self.bot = bot
        self.sessions = {}

        try:
            with open('data/diepio_guilds.txt') as f:
                self.diepio_guilds = set(map(int, map(str.strip, f)))
        except FileNotFoundError:
            self.diepio_guilds = set()

        self.bot.loop.create_task(self._init_default_trivia())

    async def _init_default_trivia(self):
        if not hasattr(self.bot, '__cached_trivia_args__'):
            await DefaultTriviaSession.initialize()
            return

        args = self.bot.__cached_trivia_args__
        now = time.monotonic()
        if now - args[-1] >= DefaultTriviaSession.TOKEN_EXPIRY_TIME:
            await DefaultTriviaSession.initialize()
            return

        DefaultTriviaSession.refresh(args)
        del self.bot.__cached_trivia_args__

    def __unload(self):
        args = DefaultTriviaSession.to_args()
        if args:
            logger.info('Caching OTDB token for reloading.')
            self.bot.__cached_trivia_args__ = args

    @contextlib.contextmanager
    def _create_session(self, ctx, session):
        key = ctx.channel.id
        self.sessions[key] = session
        try:
            yield
        finally:
            del self.sessions[key]

    async def _trivia(self, ctx, cls):
        if ctx.channel.id in self.sessions:
            return await ctx.send(
                "A trivia game's in progress right now. Join in and have some fun!"
            )

        await ctx.release()
        session = cls(ctx)
        with self._create_session(ctx, session):
            await session.run()

    @commands.group(invoke_without_command=True)
    @commands.bot_has_permissions(embed_links=True)
    async def trivia(self, ctx):
        """Starts a game of trivia"""
        await self._trivia(ctx, DefaultTriviaSession)

    @trivia.command(name='stop', aliases=['quit'])
    async def trivia_stop(self, ctx):
        """Stops trivia"""
        try:
            inst = self.sessions[ctx.channel.id]
        except KeyError:
            await ctx.send("There's no trivia game to stop.")
        else:
            inst.stop()

    @trivia.command(name='otdb', cls=DeprecatedCommand, instead='trivia')
    @commands.bot_has_permissions(embed_links=True)
    async def trivia_otdb(self, ctx):
        """Deprecated, use `{prefix}trivia` instead"""
        await ctx.invoke(self.trivia)

    if DiepioTriviaSession._questions:
        @commands.check(lambda ctx: ctx.guild.id in ctx.cog.diepio_guilds)
        @trivia.command(name='diepio', hidden=True)
        @commands.bot_has_permissions(embed_links=True)
        async def trivia_diepio(self, ctx):
            """Starts a game of diep.io trivia"""
            await self._trivia(ctx, DiepioTriviaSession)

    if os.path.isdir(POKEMON_PATH):
        @trivia.command(name='pokemon')
        @commands.bot_has_permissions(embed_links=True, attach_files=True)
        async def trivia_pokemon(self, ctx):
            """Starts a game of "Who's That Pokemon?" """
            await self._trivia(ctx, PokemonTriviaSession)

    else:
        logger.warn(f'{POKEMON_PATH} directory not found. Could not add Pokemon Trivia.')


def setup(bot):
    bot.add_cog(Trivia(bot))

def teardown(bot):
    coro = DefaultTriviaSession._session.close()
    if inspect.isawaitable(coro):
        asyncio.ensure_future(coro)
