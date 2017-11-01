import asyncio
import asyncpg
import asyncqlio
import collections
import contextlib
import discord
import enum
import functools
import glob
import io
import itertools
import json
import logging
import os
import random

from difflib import SequenceMatcher
from discord.ext import commands
from html import unescape

from . import manager

from ..tables.base import TableBase
from ..utils.misc import base_filename, emoji_url

from core.cog import Cog


_logger = logging.getLogger(__name__)

TIMEOUT_ICON = emoji_url('\N{ALARM CLOCK}')


class Category(TableBase, table_name='trivia_categories'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)
    guild_id = asyncqlio.Column(asyncqlio.BigInt)
    guild_id_idx = asyncqlio.Index(guild_id)

    name = asyncqlio.Column(asyncqlio.String(256))
    description = asyncqlio.Column(asyncqlio.String(512), nullable=True)

    # ---------Converter stuffs------------

    _default_categories = {}

    @classmethod
    async def convert(cls, ctx, arg):
        lowered = arg.lower()
        with contextlib.suppress(KeyError):
            return cls._default_categories[lowered]

        query = ctx.session.select.from_(cls).where((cls.guild_id == ctx.guild.id)
                                                    & (cls.name == lowered))
        result = await query.first()
        if result is None:
            raise commands.BadArgument(f"Category {lowered} doesn't exist... :(")

        return result


class Question(TableBase, table_name='trivia_questions'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)
    category_id = asyncqlio.Column(asyncqlio.Integer, foreign_key=asyncqlio.ForeignKey(Category.id))
    category_id_idx = asyncqlio.Index(category_id)

    question = asyncqlio.Column(asyncqlio.String(1024))
    answer = asyncqlio.Column(asyncqlio.String(512))
    image = asyncqlio.Column(asyncqlio.String(512), nullable=True)


# Helper classes for DefaultTrivia, because we don't want to lug all those dicts around.
_QuestionTuple = collections.namedtuple('_QuestionTuple', 'question answer image')
_QuestionTuple.__new__.__defaults__ = (None, )
_CategoryTuple = collections.namedtuple('_DefaultCategoryTuple', 'name description questions')
_CategoryTuple.__new__.__defaults__ = (None, None, )


class BaseTriviaSession:
    """A base class for all Trivia games.

    Subclasses must implement the next_question method.
    """
    def __init__(self, ctx, category):
        self.ctx = ctx
        self.category = category
        self._current_question = None
        self._answered = asyncio.Event()
        self._scoreboard = collections.Counter()

    def _check_answer(self, message):
        if message.channel != self.ctx.channel:
            return False
        # Prevent other bots from accidentally answering the question
        # This issue has happened numberous times with other bots.
        if message.author.bot:
            return False

        self._answered.set()
        sm = SequenceMatcher(None, message.content.lower(), self._current_question.answer.lower())
        return sm.ratio() >= .85

    async def _show_question(self, n):
        leader = self.leader
        leader_text = f'{leader[0]} with {leader[1]} points' if leader else None
        description = self.category.description or discord.Embed.Empty

        embed = (discord.Embed(description=description, colour=random.randint(0, 0xFFFFFF))
                 .set_author(name=self.category.name or 'Trivia')
                 .add_field(name=f'Question #{n}', value=self._current_question.question)
                 .set_footer(text=f'Current leader: {leader_text}')
                 )

        await self.ctx.send(embed=embed)

    async def _show_timeout(self):
        answer = self._current_question.answer
        embed = (discord.Embed(description=f'The answer was **{answer}**', colour=0xFF0000)
                 .set_author(name='Times up!', icon_url=TIMEOUT_ICON)
                 .set_footer(text='No one got any points :(')
                 )

        await self.ctx.send(embed=embed)

    async def _show_answer(self, answerer, action):
        description = f'The answer was **{self._current_question.answer}**.'

        embed = (discord.Embed(colour=0x00FF00, description=description)
                 .set_author(name=f'{answerer} {action}!')
                 .set_thumbnail(url=answerer.avatar_url)
                 .set_footer(text=f'{answerer} now has {self._scoreboard[answerer]} points.')
                 )

        await self.ctx.send(embed=embed)

    async def _loop(self):
        get_answer = functools.partial(self.ctx.bot.wait_for, 'message',
                                       timeout=20, check=self._check_answer)

        for q in itertools.count(1):
            self._current_question = await self.next_question()
            await self._show_question(q)

            try:
                message = await get_answer()
            except asyncio.TimeoutError:
                await self._show_timeout()
            else:
                user = message.author
                self._scoreboard[user] += 1
                if self._scoreboard[user] >= 10:
                    await self._show_answer(user, 'wins the game')
                    return user

                await self._show_answer(user, 'got it')

            finally:
                await asyncio.sleep(random.uniform(1.5, 3))

    async def run(self):
        self._runner = asyncio.ensure_future(self._loop())
        return await self._runner

    def stop(self):
        self._runner.cancel()

    @property
    def leader(self):
        leaderboard = self.leaderboard
        return leaderboard[0] if leaderboard else None

    @property
    def leaderboard(self):
        return self._scoreboard.most_common()

    async def next_question(self):
        raise NotImplementedError


class DefaultTriviaSession(BaseTriviaSession):
    async def next_question(self):
        return random.choice(self.category.questions)


class CustomTriviaSession(BaseTriviaSession):
    """Trivia Game using custom categories. A DB is used here."""
    async def next_question(self):
        query = """SELECT question, answer, image
                   FROM trivia_questions
                   WHERE category_id={category_id}
                   OFFSET FLOOR(RANDOM() * (
                        SELECT COUNT(*)
                        FROM trivia_questions
                        WHERE category_id={category_id}
                   ))
                   LIMIT 1
                """
        params = {'category_id': self.category.id}
        return _QuestionTuple(**await self.ctx.session.fetch(query, params))


_otdb_category = _CategoryTuple(name='Trivia - OTDB',
                                description='[Check out their site here!](https://opentdb.com)')


class _OTDBQuestion(collections.namedtuple('_OTDBQuestion', 'category type question answer incorrect')):
    @property
    def answers(self):
        return [self.answer, *self.incorrect]

    @property
    def choices(self):
        a = self.answers
        return random.sample(a, len(a))

    @classmethod
    def from_data(cls, question):
        return cls(
            category=question['category'],
            type=question['type'],
            question=unescape(question['question']),
            answer=unescape(question['correct_answer']),
            incorrect=tuple(map(unescape, question['incorrect_answers'])),
        )

# How many times should the cache be used before making an API request
# to get more questions, the lower this number, the more likely it will
# make an HTTP request. Set to 0 to always use the API
#
# Note that the toggler is only called when the trivia session doesn't
# have any questions in the queue, so be careful when making this really
# high. Otherwise the question cache might never be filled.
TIMES_TO_USE_CACHE = 2

# The size the cache should be before a new session primes a new session
# using the cache rather than using the global toggler.
MIN_CACHE_SIZE = 1000


class OTDBTriviaSession(BaseTriviaSession):
    # Global toggler for whether to use the cache or not
    _toggle_using_cache = itertools.cycle([False] + [True] * TIMES_TO_USE_CACHE).__next__
    _question_cache = set()

    def __init__(self, ctx, category=None):
        super().__init__(ctx, _otdb_category)
        self._pending = collections.deque(maxlen=50)

        if len(self._question_cache) >= MIN_CACHE_SIZE:
            # Only prime 10 questions, because it's rare for a trivia game to go 
            # longer than that. So pre-filling the pending queue with any more
            # questions would just be a waste.
            self._pending.extend(random.sample(self._question_cache, 10))

    def _check_answer(self, message):
        # There must break early.
        if message.channel != self.ctx.channel:
            return False
        # Prevent other bots from accidentally answering the question
        # This issue has happened numberous times with other bots.
        if message.author.bot:
            return False

        if not message.content.isdigit():
            # Do not allow negative numbers because they're not gonna be
            # listed in the answers. We don't wanna confuse users here.
            return super()._check_answer(message)

        number = int(message.content)

        try:
            choice = self._choices[number - 1]
        except IndexError:
            return super()._check_answer(message)

        return choice == self._current_question.answer

    async def _show_question(self, n):
        question = self._current_question
        self._choices = question.choices

        leader = self.leader
        leader_text = f'{leader[0]} with {leader[1]} points' if leader else None
        description = self.category.description

        is_tf = question.type == 'boolean'
        tf_header = '**True or False**\n' * is_tf
        question_field = f'{tf_header}{question.question}'
        possible_answers = '\n'.join(itertools.starmap('{0}. {1}'.format, enumerate(self._choices, 1)))

        embed = (discord.Embed(description=description, colour=random.randint(0, 0xFFFFFF))
                 .set_author(name=self.category.name)
                 .add_field(name='Category', value=question.category, inline=False)
                 .add_field(name=f'Question #{n}', value=question_field, inline=False)
                 .set_footer(text=f'Current leader: {leader_text}')
                 )
        if not is_tf:
            embed.add_field(name='Possible answers', value=possible_answers, inline=True)

        await self.ctx.send(embed=embed)

    async def next_question(self):
        try:
            question = self._pending.pop()
        except IndexError:
            # Make sure the global cache is non-empty, because if the toggler
            # is True and there are no questions in the cache, then the pop
            # at the bottom will error out. And the trivia would end mysteriously.
            if self._toggle_using_cache() and self._question_cache:
                results = random.sample(self._question_cache, 50)
            else:
                async with self.ctx.bot.session.get('https://opentdb.com/api.php?amount=50') as resp:
                    data = await resp.json()

                results = list(map(_OTDBQuestion.from_data, data['results']))
                self._question_cache.update(results)

            self._pending.extend(results)

            question = self._pending.pop()

        return question


class RandomQuestionType(enum.Enum):
    DEFAULT = enum.auto()
    CUSTOM = enum.auto()
    OTDB = enum.auto()


RQT_CHOICES = list(RandomQuestionType)
RQT_NO_CUSTOM = [RandomQuestionType.DEFAULT, RandomQuestionType.OTDB]


class RandomTriviaSession(OTDBTriviaSession):
    """Trivia Game using ALL categories, both custom, default AND OTDB."""
    _toggle_using_cache = OTDBTriviaSession._toggle_using_cache
    _question_cache = OTDBTriviaSession._question_cache

    def _check_answer(self, message):
        if self._question_type == RandomQuestionType.OTDB:
            return super()._check_answer(message)
        return BaseTriviaSession._check_answer(self, message)

    async def _show_question(self, n):
        if self._question_type == RandomQuestionType.OTDB:
            await super()._show_question(n)
        else:
            await BaseTriviaSession._show_question(self, n)

    async def next_question(self):
        self._question_type = qt = random.choice(RQT_CHOICES)

        if qt == RandomQuestionType.CUSTOM:
            # Try using custom categories first, we'll need to use
            # default categories as a fallback in case the server
            # doesn't have any custom categories.
            query = """ SELECT *
                        FROM trivia_categories
                        WHERE guild_id={guild_id}
                        OFFSET FLOOR(RANDOM() * (
                            SELECT COUNT(*)
                            FROM trivia_categories
                            WHERE guild_id={guild_id}
                        ))
                        LIMIT 1
                    """
            params = {'guild_id': self.ctx.guild.id}
            try:
                row = await self.ctx.session.fetch(query, params)
            except Exception:
                # Table doesn't exist, ignore it.
                self._question_type = qt = random.choice(RQT_NO_CUSTOM)
                _logger.exception('Could not select a trivia question from PostgreSQL.')
            else:
                if row:
                    self.category = Category(**row)
                    return await CustomTriviaSession.next_question(self)
                else:
                    self._question_type = qt = random.choice(RQT_NO_CUSTOM)

        if qt == RandomQuestionType.DEFAULT and Category._default_categories:
            self.category = random.choice(list(Category._default_categories.values()))
            return await DefaultTriviaSession.next_question(self)
        else:
            self.category = _otdb_category
            return await OTDBTriviaSession.next_question(self)

def _process_json(d, *, name=''):
    name = d.pop('name', name)
    description = d.pop('description', None)
    questions = tuple(_QuestionTuple(**q) for q in d['questions'])

    return _CategoryTuple(name, description, questions)


def _load_category_from_file(filename):
    with open(filename) as f:
        cat = json.load(f)
        name = base_filename(filename)
        return _process_json(cat, name=name)


# ------------- The actual cog --------------------

class Trivia(Cog):
    FILE_PATH = os.path.join('.', 'data', 'games', 'trivia')

    def __init__(self, bot):
        self.bot = bot
        self.manager = manager.SessionManager()

        self.bot.loop.create_task(self._load_default_categories())

    async def _load_default_categories(self):
        load_async = functools.partial(self.bot.loop.run_in_executor,
                                       None, _load_category_from_file)
        load_tasks = map(load_async, glob.iglob(f'{self.FILE_PATH}/*.json'))
        categories = await asyncio.gather(*load_tasks)

        Category._default_categories.update((c.name, c) for c in categories)
        print('everything is ok now')

    async def _run_trivia(self, ctx, category, cls):
        with self.manager.temp_session(ctx.channel, cls(ctx, category)) as inst:
            await inst.run()
            await asyncio.sleep(1.5)

    @commands.group(invoke_without_command=True)
    async def trivia(self, ctx, *, category: Category = None):
        """Starts a game of trivia. 

        Specifying no category will choose from all categories.
        This means it will choose from OTDB, the server's custom
        categories, and the built-in categories.
        """
        cls = (
            DefaultTriviaSession if isinstance(category, _CategoryTuple) 
            else RandomTriviaSession if category is None 
            else CustomTriviaSession
        )

        await self._run_trivia(ctx, category, cls)

    @trivia.command(name='otdb')
    async def trivia_otdb(self, ctx):
        """Starts a game of trivia using the Open Trivia Database.
        (https://opentdb.com/)
        """
        await self._run_trivia(ctx, None, OTDBTriviaSession)

    @trivia.command(name='stop')
    async def trivia_stop(self, ctx):
        """Stops a game of trivia."""
        game = self.manager.get_session(ctx.channel)
        if game is None:
            return await ctx.send("There is no trivia game to stop... :|")

        game.stop()
        await ctx.send("Trivia stopped...")

    # XXX: Due to possible issues and abuse, such as suggesting a 30-million
    #      question category, I'm not sure if I should keep this.
    @commands.group(name='triviacat', aliases=['tcat'], enabled=False)
    async def trivia_category(self, ctx):
        """Commands related to adding or removing custom trivia categories.

        This **does not** start a trivia game. `{prefix}trivia` does that.
        """

    @trivia_category.command(name='add')
    async def custom_trivia_add(self, ctx):
        """Adds a custom trivia category.

        This takes a JSON attachment.
        The format of the file must be something like this:
        ```js
        {{
            "name": "name of your trivia category. if this isn't specified it uses the filename.",
            "description": "description",
            "questions": [
                {{
                    "question": "this is a question",
                    "answer": "my answer",
                    "image": "optional link to an image"
                }}
            ]

        }}
        ```
        """
        try:
            attachment = ctx.message.attachments[0]
        except IndexError:
            return await ctx.send('You need an attachment.')

        with io.BytesIO() as file:
            await attachment.save(file)
            file.seek(0)
            category = _process_json(json.load(file), name=attachment.filename)

        row = await ctx.session.add(Category(
            guild_id=ctx.guild.id,
            name=category.name,
            description=category.description,
        ))

        columns = ('category_id', 'question', 'answer', 'image')
        to_insert = [(row.id, *q) for q in category.questions]

        conn = ctx.session.transaction.acquired_connection
        await conn.copy_records_to_table('trivia_questions', columns=columns, records=to_insert)
        await ctx.send('\N{OK HAND SIGN}')

    @trivia_category.command(name='remove')
    async def trivia_category_remove(self, ctx, name):
        """Removes a custom trivia category."""
        await (ctx.session.delete.table(Category)
                         .where((Category.guild_id == ctx.guild.id)
                                & (Category.name == name))
               )
        await ctx.send('\N{OK HAND SIGN}')


def setup(bot):
    bot.add_cog(Trivia(bot))