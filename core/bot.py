import aiohttp
import asyncio
import asyncpg
import collections
import contextlib
import discord
import emoji
import inspect
import json
import logging
import random
import re
import sys
import textwrap
import traceback

from datetime import datetime
from discord.ext import commands
from more_itertools import always_iterable

from . import context, errors
from .cog import Cog

from cogs.utils.jsonf import JSONFile
from cogs.utils.scheduler import DatabaseScheduler
from cogs.utils.time import duration_units
from cogs.utils.transformdict import CIDict

# The bot's config file
import config

log = logging.getLogger(__name__)
command_log = logging.getLogger('commands')


def _is_submodule(parent, child):
    return parent == child or child.startswith(parent + ".")


class _UnicodeEmoji(discord.PartialEmoji):
    __slots__ = ()

    def __new__(cls, name):
        return super().__new__(cls, animated=False, name=name, id=None)

    @property
    def url(self):
        hexes = '-'.join(hex(ord(c))[2:] for c in str(self))
        return f'https://twemoji.maxcdn.com/2/72x72/{hexes}.png'


async def _set_codec(conn):
    await conn.set_type_codec(
            'jsonb',
            schema='pg_catalog',
            encoder=json.dumps,
            decoder=json.loads,
            format='text'
    )


async def _create_pool(dsn, *, init=None, **kwargs):
    # credits to danny
    if init is None:
        async def new_init(conn):
            await _set_codec(conn)
    else:
        async def new_init(conn):
            await _set_codec(conn)
            await init(conn)

    return await asyncpg.create_pool(dsn, init=new_init, **kwargs)


_MINIMAL_PERMISSIONS = [
    'send_messages',
    'embed_links',
    'add_reactions',
    'attach_files'
    "use_external_emojis",
]

_FULL_PERMISSIONS = [
    *_MINIMAL_PERMISSIONS,
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "kick_members",
    "ban_members",
    "create_instant_invite",

    "manage_messages",
    "read_message_history",

    "mute_members",
    "deafen_members",
]

def _make_permissions(*permissions):
    perms = discord.Permissions.none()
    perms.update(**dict.fromkeys(permissions, True))
    return perms

_MINIMAL_PERMISSIONS = _make_permissions(*_MINIMAL_PERMISSIONS)
_FULL_PERMISSIONS = _make_permissions(*_FULL_PERMISSIONS)
del _make_permissions


MAX_FORMATTER_WIDTH = 90

def _callable_prefix(bot, message):
    if message.guild:
        prefixes = bot.custom_prefixes.get(message.guild.id, bot.default_prefix)
    else:
        prefixes = bot.default_prefix

    return commands.when_mentioned_or(*prefixes)(bot, message)


VersionInfo = collections.namedtuple('VersionInfo', 'major minor micro')


# Activity-related stuffs...
def _parse_type(type_):
    with contextlib.suppress(AttributeError):
        type_ = type_.lower()

    try:
        return discord.ActivityType[type_]
    except KeyError:
        pass

    typ_ = discord.enums.try_enum(discord.ActivityType, type_)
    if typ_ is type_:
        raise ValueError(f'inappropriate activity type passed: {type_!r}')
    return typ_

def _get_proper_activity(type, name, url=''):
    if type is discord.ActivityType.playing:
        return discord.Game(name=name)
    if type is discord.ActivityType.streaming:
        # TODO: Validate twitch.tv url
        return discord.Streaming(name=name, url=url)
    return discord.Activity(type=type, name=name)


class Chiaki(commands.Bot):
    __version__ = '1.1.0'
    version_info = VersionInfo(major=1, minor=1, micro=0)

    def __init__(self):
        super().__init__(command_prefix=_callable_prefix,
                         description=config.description,
                         pm_help=None)

        # Case-insensitive dict so we don't have to override get_cog
        # or get_cog_commands
        self.cogs = CIDict()

        # loop is needed to prevent outside coro errors
        self.session = aiohttp.ClientSession(loop=self.loop)
        self.table_base = None

        try:
            with open('data/command_image_urls.json') as f:
                self.command_image_urls = __import__('json').load(f)
        except FileNotFoundError:
            self.command_image_urls = {}

        self.message_counter = 0
        self.command_counter = collections.Counter()
        self.custom_prefixes = JSONFile('customprefixes.json')

        self.reset_requested = False

        psql = f'postgresql://{config.psql_user}:{config.psql_pass}@{config.psql_host}/{config.psql_db}'
        self.pool = self.loop.run_until_complete(_create_pool(psql, command_timeout=60))

        self.db_scheduler = DatabaseScheduler(self.pool, timefunc=datetime.utcnow)
        self.db_scheduler.add_callback(self._dispatch_from_scheduler)

        for ext in config.extensions:
            # Errors should never pass silently, if there's a bug in an extension,
            # better to know now before the bot logs in, because a restart
            # can become extremely expensive later on, especially with the
            # 1000 IDENTIFYs a day limit.
            self.load_extension(ext)

        self._game_task = self.loop.create_task(self.change_game())

    def _import_emojis(self):
        import emojis

        d = {}

        # These are not recognized by the Unicode or Emoji standard, but
        # discord never really was one to follow standards.
        is_edge_case_emoji = {
           *(chr(i + 0x1f1e6) for i in range(26)),
           *(f'{i}\u20e3' for i in [*range(10), '#', '*']),
        }.__contains__

        def parse_emoji(em):
            if isinstance(em, int) and not isinstance(em, bool):
                return self.get_emoji(em)

            if isinstance(em, str):
                match = re.match(r'<:[a-zA-Z0-9\_]+:([0-9]+)>$', em)
                if match:
                    return self.get_emoji(int(match[1]))
                if em in emoji.UNICODE_EMOJI or is_edge_case_emoji(em):
                    return _UnicodeEmoji(name=em)
                log.warn('Unknown Emoji: %r', em)

            return em

        for name, em in inspect.getmembers(emojis):
            if name[0] == '_':
                continue

            if hasattr(em, '__iter__') and not isinstance(em, str):
                em = list(map(parse_emoji, em))
            else:
                em = parse_emoji(em)

            d[name] = em

        del emojis  # break reference to module for easy reloading
        self.emoji_config = collections.namedtuple('EmojiConfig', d)(**d)

    def _dispatch_from_scheduler(self, entry):
        self.dispatch(entry.event, entry)

    async def close(self):
        await self.session.close()
        self._game_task.cancel()
        await super().close()

    def add_cog(self, cog):
        if not isinstance(cog, Cog):
            raise discord.ClientException(f'cog must be an instance of {Cog.__qualname__}')
        super().add_cog(cog)

        self.cogs.update(dict.fromkeys(cog.__aliases__, cog))
        self.cogs[cog.__class__.name] = cog

    def remove_cog(self, name):
        real_cog = self.cogs.get(name)
        if real_cog is None:
            return

        super().remove_cog(name)

        # remove cog aliases
        cogs_to_remove = [name for name, cog in self.cogs.items() if cog is real_cog]
        for name in cogs_to_remove:
            del self.cogs[name]

    @contextlib.contextmanager
    def temp_listener(self, func, name=None):
        """Context manager for temporary listeners"""
        self.add_listener(func, name)
        try:
            yield
        finally:
            self.remove_listener(func)

    def __format_name_for_activity(self, name):
        return name.format(
            server_count=self.guild_count,
            user_count=self.user_count,
            version=self.__version__,
        )

    def __parse_activity(self, game):
        if isinstance(game, str):
            return discord.Game(name=self.__format_name_for_activity(game))

        if isinstance(game, collections.abc.Sequence):
            type_, name, url = (*game, config.twitch_url)[:3]  # not accepting a seq of just "[type]"
            type_ = _parse_type(type_)
            name = self.__format_name_for_activity(name)
            return _get_proper_activity(type_, name, url)

        if isinstance(game, collections.abc.Mapping):
            def get(key):
                try:
                    return game[key]
                except KeyError:
                    raise ValueError(f"game mapping must have a {key!r} key, got {game!r}")

            data = {
                **game,
                'type': _parse_type(get('type')),
                'name': self.__format_name_for_activity(get('name'))
            }
            data.setdefault('url', config.twitch_url)
            return _get_proper_activity(**data)

        raise TypeError(f'expected a str, sequence, or mapping for game, got {type(game).__name__!r}')

    async def change_game(self):
        await self.wait_until_ready()
        while True:
            pick = random.choice(config.games)
            try:
                activity = self.__parse_activity(pick)
            except (TypeError, ValueError):
                log.exception(f'inappropriate game {pick!r}, removing it from the list.')
                config.games.remove(pick)

            await self.change_presence(activity=activity)
            await asyncio.sleep(random.uniform(0.5, 2) * 60)

    def run(self):
        super().run(config.token, reconnect=True)

    def get_guild_prefixes(self, guild):
        proxy_msg = discord.Object(id=None)
        proxy_msg.guild = guild
        return _callable_prefix(self, proxy_msg)

    def get_raw_guild_prefixes(self, guild):
        return self.custom_prefixes.get(guild.id, self.default_prefix)

    async def set_guild_prefixes(self, guild, prefixes):
        prefixes = prefixes or []
        if len(prefixes) > 10:
            raise RuntimeError("You have too many prefixes you indecisive goof!")

        await self.custom_prefixes.put(guild.id, sorted(set(prefixes), reverse=True))

    async def process_commands(self, message):
        ctx = await self.get_context(message, cls=context.Context)

        if ctx.command is None:
            return

        async with ctx.acquire():
            await self.invoke(ctx)

    async def run_sql(self):
        await self.pool.execute(self.schema)

    def _dump_schema(self):
        with open('schema.sql', 'w') as f:
            f.write(self.schema)

    async def dump_sql(self):
        await self.loop.run_in_executor(None, self._dump_schema)

    # --------- Events ----------

    async def on_ready(self):
        print('Logged in as')
        print(self.user.name)
        print(self.user.id)
        print('------')
        self._import_emojis()
        self.db_scheduler.run()

        if not hasattr(self, 'appinfo'):
            self.appinfo = (await self.application_info())

        if self.owner_id is None:
            self.owner = self.appinfo.owner
            self.owner_id = self.owner.id
        else:
            self.owner = self.get_user(self.owner_id)

        if not hasattr(self, 'creator'):
            self.creator = await self.get_user_info(239110748180054017)

        if not hasattr(self, 'start_time'):
            self.start_time = datetime.utcnow()

    async def on_command_error(self, ctx, error, *, bypass=False):
        if (
            isinstance(error, commands.CheckFailure)
            and not isinstance(error, commands.BotMissingPermissions)
            and await self.is_owner(ctx.author)
        ):
            # There is actually a race here. When this command is invoked the
            # first time, it's wrapped in a context manager that automatically
            # starts and closes a DB session.
            #
            # The issue is that this event is dispatched, which means during the
            # first invoke, it creates a task for this and goes on with its day.
            # The problem is that it doesn't wait for this event, meaning it might
            # accidentally close the session before or during this command's
            # reinvoke.
            #
            # This solution is dirty but since I'm only doing it once here
            # it's fine. Besides it works anyway.
            while ctx.db:
                await asyncio.sleep(0)

            try:
                async with ctx.acquire():
                    await ctx.reinvoke()
            except Exception as exc:
                await ctx.command.dispatch_error(ctx, exc)
            return

        if not bypass and hasattr(ctx.command, 'on_error'):
            return

        cause = error.__cause__
        if isinstance(error, errors.ChiakiException):
            await ctx.send(str(error))
        elif type(error) is commands.BadArgument:
            await ctx.send(str(cause or error))
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send('This command cannot be used in private messages.')
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f'This command ({ctx.command}) needs another parameter ({error.param})')
        elif isinstance(error, commands.CommandInvokeError):
            print(f'In {ctx.command.qualified_name}:', file=sys.stderr)
            traceback.print_tb(error.original.__traceback__)
            print(f'{error.__class__.__name__}: {error}'.format(error), file=sys.stderr)
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.bot_missing_perms(error.missing_perms)

    async def on_message(self, message):
        self.message_counter += 1

        # prevent other selfs from triggering commands
        if not message.author.bot:
            await self.process_commands(message)

    async def on_command(self, ctx):
        self.command_counter['commands'] += 1
        self.command_counter['executed in DMs'] += isinstance(ctx.channel, discord.abc.PrivateChannel)
        fmt = ('Command executed in {0.channel} ({0.channel.id}) from {0.guild} ({0.guild.id}) '
               'by {0.author} ({0.author.id}) Message: "{0.message.content}"')
        command_log.info(fmt.format(ctx))

    async def on_command_completion(self, ctx):
        self.command_counter['succeeded'] += 1

    # ------ Viewlikes ------

    # Note these views and properties look deceptive. They look like a thin 
    # wrapper len(self.guilds). However, the reason why these are here is
    # to avoid a temporary list to get the len of. Bot.guilds and Bot.users
    # creates a list which can cause a massive hit in performance later on.

    def guildsview(self):
        return self._connection._guilds.values()

    def usersview(self):
        return self._connection._users.values()

    @property
    def guild_count(self):
        return len(self._connection._guilds)

    @property
    def user_count(self):
        return len(self._connection._users)

    # ------ Config-related properties ------

    @discord.utils.cached_property
    def minimal_invite_url(self):
        return discord.utils.oauth_url(self.user.id, _MINIMAL_PERMISSIONS)

    @discord.utils.cached_property
    def invite_url(self):
        return discord.utils.oauth_url(self.user.id, _FULL_PERMISSIONS)

    @property
    def default_prefix(self):
        return always_iterable(config.command_prefix)

    @property
    def colour(self):
        return config.colour

    @property
    def webhook(self):
        wh_url = config.webhook_url
        if not wh_url:
            return None
        return discord.Webhook.from_url(wh_url, adapter=discord.AsyncWebhookAdapter(self.session))

    @discord.utils.cached_property
    def feedback_destination(self):
        dest = config.feedback_destination
        if not dest:
            return None
        if isinstance(dest, int):
            return self.get_channel(dest)
        return discord.Webhook.from_url(dest, adapter=discord.AsyncWebhookAdapter(self.session))

    # ------ misc. properties ------

    @property
    def support_invite(self):
        # The following is the link to the bot's support server.
        # You are allowed to change this to be another server of your choice.
        # However, doing so will instantly void your warranty.
        # Change this azt your own peril.
        return 'https://discord.gg/WtkPTmE'

    @property
    def uptime(self):
        return datetime.utcnow() - self.start_time

    @property
    def str_uptime(self):
        return duration_units(self.uptime.total_seconds())

    @property
    def schema(self):
        schema = ''.join(getattr(ext, '__schema__', '') for ext in self.extensions.values())
        return textwrap.dedent(schema + self.db_scheduler.__schema__)
