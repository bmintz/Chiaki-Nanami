import asyncio
import collections
import contextlib
import itertools
import json
import os
import random
from difflib import SequenceMatcher
from html import unescape

import aiohttp
import discord
from discord.ext import commands

from core.cog import Cog
from ..utils.formats import pluralize
from ..utils.misc import emoji_url


class StopTrivia(Exception):
    pass


POINTS_TO_WIN = 10
TRIVIA_ANSWER_TIMEOUT = 60
TIMEOUT_ICON = emoji_url('\N{ALARM CLOCK}')


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

    def _question_embed(self, number):
        raise NotImplementedError

    # End of the abstract methods

    def _timeout_embed(self):
        answer = self._current_question.answer
        return (discord.Embed(description=f'The answer was **{answer}**', colour=0xFF0000)
                .set_author(name='Times up!', icon_url=TIMEOUT_ICON)
                .set_footer(text='No one got any points :(')
                )

    def _answer_embed(self, user):
        score = self._score_board[user.id]
        action = 'wins the game' if score >= POINTS_TO_WIN else 'got it'
        description = f'The answer was **{self._current_question.answer}**.'

        return (discord.Embed(colour=0x00FF00, description=description)
                .set_author(name=f'{user} {action}!')
                .set_thumbnail(url=user.avatar_url)
                .set_footer(text=f'{user} now has {score} points.')
                )

    async def _loop(self):
        wait_for = self._ctx.bot.wait_for
        send = self._ctx.send

        for q in itertools.count(1):
            self._current_question = await self._get_question()
            await send(embed=self._question_embed(q))

            try:
                message = await wait_for('message', timeout=20, check=self._check)
            except asyncio.TimeoutError:
                await send(embed=self._timeout_embed())
            else:
                user = message.author
                self._score_board[user.id] += 1
                await send(embed=self._answer_embed(user))

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

    def __init__(self, ctx):
        super().__init__(ctx)

        self._choices = None
        self._pending = []
        self._answerers = set()

        if len(self.__cache) >= 500:
            # We should probably pre-fill this because most trivia games
            # don't last very long. So we don't need to make an HTTP request
            # when we don't have to.
            self._pending.extend(random.sample(self._cache, 15))

    @classmethod
    async def initialize(cls):
        if cls.__token is not None:
            return

        async with cls._session.get(cls.TOKEN_BASE, params=dict(command='request')) as r:
            response = await r.json()

        assert response['response_code'] == 0
        cls.__token = response['token']

    async def __get(self):
        if self.__token is None:
            await self.initialize()

        params = dict(amount=self.AMOUNT, token=self.__token)
        async with self._session.get(self.BASE, params=params) as r:
            response = await r.json()

        if response['response_code'] == 3:
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

    def _question_embed(self, number):
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

        return (discord.Embed(description=question, colour=random.randint(0, 0xFFFFFF))
                .set_author(name=f'Question #{number}')
                .add_field(name='Choices', value=possible_answers, inline=False)
                .add_field(name='Leader', value=leader_text, inline=False)
                .set_footer(text='Questions provided by opentdb.com')
                )


Question = collections.namedtuple('Question', 'question answer')
TANK_ICON = 'https://vignette.wikia.nocookie.net/diepio/images/f/f2/Tank_Screenshot2.png'


class DiepioTriviaSession(BaseTriviaSession):
    try:
        with open(os.path.join('.', 'data', 'games', 'trivia', 'diepio.json')) as f:
            data = json.load(f)
    except FileNotFoundError:
        _questions = []
    else:
        # Supporting old cruft from when I wanted to do various built-in categories
        _questions = [Question(**d) for d in data['questions']]

    async def _get_question(self):
        return random.choice(self._questions)

    def _check(self, message):
        if message.channel != self._ctx.channel:
            return False

        # Prevent other bots from accidentally answering the question
        # This issue has happened numberous times with other bots.
        if message.author.bot:
            return False

        self._answer_waiter.set()
        sm = SequenceMatcher(None, message.content.lower(), self._current_question.answer.lower())
        return sm.ratio() >= .85

    def _question_embed(self, number):
        question = self._current_question.question

        leader = self.leader
        leader_text = (
            f'{self._ctx.bot.get_user(leader[0])} with {leader[1]} points'
            if leader else None
        )

        return (discord.Embed(description=question, colour=random.randint(0, 0xFFFFFF))
                .set_author(name=f'Question #{number}', icon_url=TANK_ICON, url='http://diep.io')
                .set_footer(text=f'Leader: {leader_text}')
                )


class Trivia(Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sessions = {}
        try:
            with open('data/diepio_guilds.txt') as f:
                self.diepio_guilds = set(map(int, map(str.strip, f)))
        except FileNotFoundError:
            self.diepio_guilds = set()

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

        session = cls(ctx)
        with self._create_session(ctx, session):
            await session.run()

    @commands.group(invoke_without_command=True)
    async def trivia(self, ctx):
        await self._trivia(ctx, DefaultTriviaSession)

    @trivia.command(name='stop', aliases=['quit'])
    async def trivia_stop(self, ctx):
        try:
            inst = self.sessions[ctx.channel.id]
        except KeyError:
            await ctx.send("There's no trivia game to stop.")
        else:
            inst.stop()

    if DiepioTriviaSession._questions:
        @commands.check(lambda ctx: ctx.guild.id in ctx.cog.diepio_guilds)
        @trivia.command(name='diepio', hidden=True)
        async def trivia_diepio(self, ctx):
            await self._trivia(ctx, DiepioTriviaSession)


def setup(bot):
    bot.add_cog(Trivia(bot))

def teardown(bot):
    DefaultTriviaSession._session.close()
