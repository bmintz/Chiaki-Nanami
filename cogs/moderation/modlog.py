import asyncio
import collections
import contextlib
import discord
import enum
import json
import logging
import operator
import re

from datetime import datetime, timedelta
from discord.ext import commands
from functools import reduce

from ..utils import cache
from ..utils.misc import emoji_url, truncate, unique
from ..utils.paginator import EmbedFieldPages
from ..utils.time import duration_units, parse_delta

from core import errors
from core.cog import Cog

log = logging.getLogger(__name__)


class ModLogError(errors.ChiakiException):
    pass


__schema__ = """
    CREATE TABLE IF NOT EXISTS modlog (
        id SERIAL PRIMARY KEY,
        channel_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        guild_id BIGINT NOT NULL,
        action VARCHAR(16) NOT NULL,
        mod_id BIGINT NOT NULL,
        reason TEXT NOT NULL,
        extra TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS modlog_guild_id_idx ON modlog (guild_id);

    CREATE TABLE IF NOT EXISTS modlog_targets (
        id SERIAL PRIMARY KEY,
        entry_id INTEGER REFERENCES modlog ON DELETE CASCADE,
        user_id BIGINT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS modlog_config (
        guild_id BIGINT PRIMARY KEY,
        channel_id BIGINT NOT NULL DEFAULT 0,

        -- some booleans
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        log_auto BOOLEAN NOT NULL DEFAULT TRUE,
        dm_user BOOLEAN NOT NULL DEFAULT TRUE,
        poll_audit_log BOOLEAN NOT NULL DEFAULT TRUE,

        events INTEGER NOT NULL DEFAULT {default_flags}
    );
"""

ModAction = collections.namedtuple('ModAction', 'repr emoji colour')


_mod_actions = {
    'warn'    : ModAction('warned', '\N{WARNING SIGN}', 0xFFC107),
    'mute'    : ModAction('muted', '\N{SPEAKER WITH CANCELLATION STROKE}', 0x424242),
    'kick'    : ModAction('kicked', '\N{WOMANS BOOTS}', 0xFF9800),
    # XXX: These bans are all red. This won't be good for color-blind people.
    'softban' : ModAction('soft banned', '\N{BIOHAZARD SIGN}', 0xFF5722),
    'tempban' : ModAction('temporarily banned', '\N{ALARM CLOCK}', 0xf44336),
    'ban'     : ModAction('banned', '\N{HAMMER}', 0xd50000),
    'unban'   : ModAction('unbanned', '\N{DOVE OF PEACE}', 0x43A047),
    'hackban' : ModAction('prematurely banned', '\N{NO ENTRY}', 0x212121),
    'massban' : ModAction('massbanned', '\N{NO ENTRY}', 0xb71c1c),
}


class EnumConverter(enum.IntFlag):
    """Mixin used for converting enums"""
    @classmethod
    async def convert(cls, ctx, arg):
        try:
            return cls[arg.lower()]
        except KeyError:
            raise commands.BadArgument(f'{arg} is not a valid {cls.__name__}')


ActionFlag = enum.IntFlag('ActionFlag', list(_mod_actions), type=EnumConverter)
_default_flags = (2 ** len(_mod_actions) - 1) & ~ActionFlag.hackban

for k, v in list(_mod_actions.items()):
    _mod_actions[f'auto-{k}'] = v._replace(repr=f'auto-{v.repr}')

__schema__ = __schema__.format(default_flags=_default_flags.value)

MASSBAN_THUMBNAIL = emoji_url('\N{NO ENTRY}')


fields = 'channel_id enabled log_auto dm_user poll_audit_log events'
ModLogConfig = collections.namedtuple('ModLogConfig', fields)
del fields


def _is_mod_action(ctx):
    return ctx.command.qualified_name in _mod_actions


@cache.cache(maxsize=512)
async def _get_message(channel, message_id):
    o = discord.Object(id=message_id + 1)
    # don't wanna use get_message due to poor rate limit (1/1s) vs (50/1s)
    msg = await channel.history(limit=1, before=o).next()

    if msg.id != message_id:
        return None

    return msg


@cache.cache(maxsize=None, make_key=lambda a, kw: a[-1])
async def _get_number_of_cases(connection, guild_id):
    query = 'SELECT COUNT(*) FROM modlog WHERE guild_id=$1;'
    row = await connection.fetchrow(query, guild_id)
    return row['count']


class CaseNumber(commands.Converter):
    async def convert(self, ctx, arg):
        try:
            num = int(arg)
        except ValueError:
            raise commands.BadArgument("This has to be an actual number... -.-")

        if num < 0:
            num_cases = await _get_number_of_cases(ctx.db, ctx.guild.id)
            if not num_cases:
                raise commands.BadArgument('There are no cases... yet.')

            num += num_cases + 1
            if num < 0:
                # Consider it out of bounds, because accessing a negative
                # index is out of bounds anyway.
                raise commands.BadArgument("I think you're travelling a little "
                                           "too far in the past there...")
        return num


class ModLog(Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cache_cleaner = asyncio.ensure_future(self._clean_cache())
        self._cache_locks = collections.defaultdict(asyncio.Event)
        self._cache = set()

    def __unload(self):
        self._cache_cleaner.cancel()

    async def _clean_cache(self):
        # Used to clear the message cache every now and then
        while True:
            await asyncio.sleep(60 * 20)
            _get_message.cache.clear()

    async def _get_case_config(self, guild_id, *, connection=None):
        connection = connection or self.bot.pool
        query = """SELECT channel_id, enabled, log_auto, dm_user, poll_audit_log, events
                   FROM modlog_config
                   WHERE guild_id = $1
                """
        row = await connection.fetchrow(query, guild_id)
        return ModLogConfig(**row) if row else None

    async def _send_case(self, config, action, server, mod, targets, reason,
                         *, extra=None, auto=False, connection=None):
        if not (config and config.enabled and config.channel_id):
            return None

        if not config.events & ActionFlag[action]:
            return None

        if auto and not config.log_auto:
            return None

        channel = server.get_channel(config.channel_id)
        if not channel:
            raise ModLogError(f"The channel ID you specified ({config.channel_id}) doesn't exist.")

        if auto:
            action = f'auto-{action}'

        connection = connection or self.bot.pool
        # Get the case number, this is why the guild_id is indexed.
        count = await _get_number_of_cases(connection, server.id)

        # Send the case like normal
        embed = self._create_embed(count + 1, action, mod, targets, reason, extra)

        try:
            message = await channel.send(embed=embed)
        except discord.Forbidden:
            raise ModLogError(
                f"I can't send messages to {channel.mention}. Check my privileges pls..."
            )

        query = """INSERT INTO modlog (guild_id, channel_id, message_id, action, mod_id, reason, extra)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                   RETURNING id
                """

        if extra is not None:
            now = message.created_at
            delta = ((now + extra.delta) - now).total_seconds()
        else:
            delta = None

        args = (
            server.id,
            channel.id,
            message.id,
            action,
            mod.id,
            reason,
            {'args': [delta]},
        )

        return query, args

    def _create_embed(self, number, action, mod, targets, reason, extra, time=None):
        time = time or datetime.utcnow()
        action = _mod_actions[action]

        avatar_url = targets[0].avatar_url if len(targets) == 1 else MASSBAN_THUMBNAIL
        bot_avatar = self.bot.user.avatar_url

        if extra is None:
            duration_string = ''
        elif isinstance(extra, float):
            duration_string = f' for {duration_units(extra)}'
        else:
            duration_string = f' for {parse_delta(extra.delta)}'

        action_field = f'{action.repr.title()}{duration_string} by {mod}'
        reason = reason or 'No reason. Please enter one.'

        return (discord.Embed(color=action.colour, timestamp=time)
                .set_author(name=f"Case #{number}", icon_url=emoji_url(action.emoji))
                .set_thumbnail(url=avatar_url)
                .add_field(name=f'User{"s" * (len(targets) != 1)}', value=', '.join(map(str, targets)))
                .add_field(name="Action", value=action_field, inline=False)
                .add_field(name="Reason", value=reason, inline=False)
                .set_footer(text=f'ID: {mod.id}', icon_url=bot_avatar)
                )

    async def _insert_case(self, guild_id, targets, query, args, connection=None):
        connection = connection or self.bot.pool

        if len(targets) == 1:
            q = f"""WITH modlog_insert as ({query})
                    INSERT INTO modlog_targets (entry_id, user_id)
                    VALUES ((SELECT id FROM modlog_insert), ${len(args) + 1})
                """
            await connection.execute(q, *args, targets[0].id)
        else:
            entry_id = await connection.execute(query, *args)
            columns = ('entry_id', 'user_id')
            to_insert = [(entry_id, t.id) for t in targets]

            await connection.copy_records_to_table('modlog_targets', columns=columns, records=to_insert)

        # Because we've successfully added a new case by this point,
        # the number of cases is no longer accurate.
        _get_number_of_cases.invalidate(None, guild_id)

    async def _notify_user(self, config, action, server, user, targets, reason,
                           extra=None, auto=False):
        if action == 'massban':
            # XXX: Should I DM users who were massbanned?
            return

        if config and not config.dm_user:
            return

        # Should always be true because we're not DMing users in a massban.
        assert len(targets) == 1, f'too many targets for {action}'

        mod_action = _mod_actions[action]
        action_applied = f'You were {mod_action.repr}'
        if extra:
            # TODO: Get the warn number.
            action_applied += ' for {duration_units(extra)}'

        # Will probably refactor this later.
        embed = (discord.Embed(colour=mod_action.colour, timestamp=datetime.utcnow())
                 .set_author(name=f'{action_applied}!', icon_url=emoji_url(mod_action.emoji))
                 .add_field(name='In', value=str(server), inline=False)
                 .add_field(name='By', value=str(user), inline=False)
                 .add_field(name='Reason', value=reason, inline=False)
                 )

        for target in targets:
            with contextlib.suppress(discord.HTTPException):
                await target.send(embed=embed)

    def _add_to_cache(self, name, guild_id, member_id, *, seconds=2):
        args = (name, guild_id, member_id)
        self._cache.add(args)
        self._cache_locks[name, guild_id, member_id].set()

        async def delete_value():
            await asyncio.sleep(seconds)
            self._cache.discard(args)
            self._cache_locks.pop((name, guild_id, member_id), None)

        self.bot.loop.create_task(delete_value())

    # Invoked by the mod-cog, this is used to wait for the cache during
    # tempban and mute completion.
    def wait_for_cache(self, name, guild_id, member_id):
        return self._cache_locks[name, guild_id, member_id].wait()

    async def on_tempban_complete(self, timer):
        # We need to prevent unbanning from accidentally triggering the manual
        # unban from being logged.
        self._add_to_cache('tempban', *timer.args)

    # These invokers are used for the Moderator cog.
    async def mod_before_invoke(self, ctx):
        # We only want to put the result on the cache iff the command succeeded parsing
        # It's ok if the command fails, we'll just handle it in on_command_error
        name = ctx.command.qualified_name
        if name not in _mod_actions:
            return

        targets = (m for m in ctx.args if isinstance(m, discord.Member))
        for member in targets:
            self._add_to_cache(name, ctx.guild.id, member.id)

    async def mod_after_invoke(self, ctx):
        name = ctx.command.qualified_name
        if name not in _mod_actions:
            return

        if ctx.command_failed:
            return

        targets = [m for m in ctx.args if isinstance(m, discord.Member)]
        # Will be set by warn in the event of auto-punishment
        auto = getattr(ctx, 'auto_punished', False)
        # For mutes and tempbans.
        extra = ctx.args[3] if 'duration' in ctx.command.params else None
        # In the event of a massban, the reason is a required positional argument
        # rather than a keyword-only consume rest one.
        reason = ctx.kwargs.get('reason') or ctx.args[2]
        if reason is not None:
            # The reason in commands is a User#0000 \N{EM DASH} reason.
            # We just want the original reason for mod logs. Since usernames
            # can't have # we can just regex it out.
            #
            # The reason why it's in this format is to provide an easy
            # and convenient format for other mod-logging bots.
            match = re.search('#[0-9]{4} \N{EM DASH} (.*)', reason)
            if match:
                reason = match[1]

        # We have get the config outside the two functions because we use it twice.
        config = await self._get_case_config(ctx.guild.id, connection=ctx.db)
        args = [config, name, ctx.guild, ctx.author, targets, reason]

        # XXX: I'm not sure if I should DM the user before or *after* the
        #      action has been applied. I currently have it done after, because
        #      the target should only be DMed if the command was executed
        #      successfully, and we can't check if it worked before we do
        #      the thing.
        await self._notify_user(*args, extra=extra, auto=auto)

        try:
            query_args = await self._send_case(
                *args,
                extra=extra,
                auto=auto,
                connection=ctx.db
            )
        except ModLogError as e:
            await ctx.send(f'{ctx.author.mention}, {e}')
        else:
            if query_args:
                query, args = query_args
                await self._insert_case(
                    connection=ctx.db,
                    guild_id=ctx.guild.id,
                    targets=targets,
                    query=query,
                    args=args
                )

    async def _poll_audit_log(self, guild, user, *, action):
        if (action, guild.id, user.id) in self._cache:
            # Assume it was invoked by a command (only commands will put this in the cache).
            return

        with contextlib.suppress(AttributeError):  # in case guild.me is a User for some reason
            if not guild.me.guild_permissions.view_audit_log:
                # early out
                return

        config = await self._get_case_config(guild.id)

        if not (config and config.poll_audit_log):
            return

        # poll the audit log for some nice shit
        # XXX: This doesn't catch softbans.
        audit_action = discord.AuditLogAction[action]

        # We'll try to be generous with delays because discord is a good service:tm:
        # Seriously some guilds might have large latency with audit logs, meaning the
        # could've added the entry way before the event is called.
        after = datetime.utcnow() - timedelta(seconds=2)

        try:
            for attempt in range(3):
                # This delay is here for two reasons:
                # 1. We want to avoid rate-limiting the bot too hard.
                # 2. We'll wait for long periods of time so that we can sufficiently
                #    wait for the audit log entry to be added, we don't know what
                #    the delay is, but we'll take a best guess
                #
                # It shouldn't take too long... Right, Discord?
                await asyncio.sleep(0.5 * (attempt + 1))  # cruddy backoff

                entry = await guild.audit_logs(action=audit_action, after=after).get(target=user)
                if entry is not None:
                    break

            else:  # hooray for for-else
                log.info('%s (ID: %d) in guild %s (ID: %d) never had an entry for event %r',
                         user, user.id, guild, guild.id, action)
                # We should just give up here. Because we need a non-None entry,
                # and in the case of member_remove, the member could've just up
                # and left the server, which means it won't make sense for it to
                # be logged.
                return

        except discord.Forbidden:
            return  # should not happen but this is here just in case it happens

        with contextlib.suppress(ModLogError):
            targets = [entry.target]
            query_args = await self._send_case(
                config,
                action,
                guild,
                entry.user,
                targets,
                entry.reason
            )

            if query_args:
                query, args = query_args
                await self._insert_case(
                    connection=self.bot.pool,
                    guild_id=guild.id,
                    targets=targets,
                    query=query,
                    args=args
                )

    async def _poll_ban(self, guild, user, *, action):
        if ('softban', guild.id, user.id) in self._cache:
            return
        if ('tempban', guild.id, user.id) in self._cache:
            return
        await self._poll_audit_log(guild, user, action=action)

    async def on_member_ban(self, guild, user):
        await self._poll_ban(guild, user, action='ban')

    async def on_member_unban(self, guild, user):
        await self._poll_ban(guild, user, action='unban')

    async def on_member_remove(self, member):
        await self._poll_audit_log(member.guild, member, action='kick')

    # ------------------- something ------------------

    async def _get_case(self, guild_id, num, *, connection):
        query = 'SELECT * FROM modlog WHERE guild_id = $1 ORDER BY id OFFSET $2 LIMIT 1;'
        return await connection.fetchrow(query, guild_id, num - 1)

    # ----------------- Now for the commands. ----------------------

    @commands.group(invoke_without_command=True)
    async def case(self, ctx, num: CaseNumber = None):
        """Group for all case searching commands. If given a number,
        it retrieves the case with the given number.

        If no number is given, it shows the latest case.

        Negative numbers are allowed. They count starting from
        the most recent case. e.g. -1 will show the newest case,
        and -10 will show the 10th newest case.
        """

        # Solving some weird nasty edge cases first
        if num is None:
            cases = await _get_number_of_cases(ctx.db, ctx.guild.id)
            if not cases:
                return await ctx.send('There are no cases here.')

        if num == 0:
            num = 1

        case = await self._get_case(ctx.guild.id, num, connection=ctx.db)
        if case is None:
            return await ctx.send(f'Case #{num} is not a valid case.')

        query = 'SELECT user_id FROM modlog_targets WHERE entry_id = $1'
        targets = [
            ctx.bot.get_user(r[0]) or f'<Unknown: {r[0]}>'
            for r in await ctx.db.fetch(query, case['id'])
        ]

        extra = json.loads(case['extra'])
        extra = extra['args'][0] if extra else None

        # Parse the cases accordingly
        embed = self._create_embed(
            num,
            case['action'],
            ctx.bot.get_user(case['mod_id']),
            targets,
            case['reason'],
            extra,
            discord.utils.snowflake_time(case['message_id']),
        )

        await ctx.send(embed=embed)

    @case.command(name='user', aliases=['member'])
    async def case_user(self, ctx, *, member: discord.Member):
        """Retrives all the cases for a specific member.

        Only members who are in the server can be searched.
        """

        # Major credit to Cute#0313 for helping me with the query for this. <3
        query = """SELECT message_id, action, mod_id, reason
                   FROM modlog, modlog_targets
                   WHERE modlog.id = modlog_targets.entry_id
                   AND guild_id = $1
                   AND user_id = $2
                   ORDER BY modlog.id;
                """

        results = await ctx.db.fetch(query, ctx.guild.id, member.id)

        get_time = discord.utils.snowflake_time
        get_user = ctx.bot.get_user

        entries = []
        for message_id, action, mod_id, reason in results:
            action = _mod_actions[action]
            name = f'{action.emoji} {action.repr.title()}'
            formatted = (
                f"**On:** {get_time(message_id) :%x %X}\n"
                # Gotta use triple-quotes to keep the syntax happy.
                f"""**Moderator:** {get_user(mod_id) or f'<Unknown ID: {mod_id}'}\n"""
                f"**Reason:** {truncate(reason, 512, '...')}\n"
                "-------------------"
            )

            entries.append((name, formatted))

        if not entries:
            yay = f'{member} has a clean record! Give them a medal or a cookie or something! ^.^'
            return await ctx.send(yay)

        pages = EmbedFieldPages(
            ctx, entries,
            title=f'Cases for {member}',
            description=f'{member} has {len(entries)} cases',
            colour=member.colour,
            inline=False
        )

        await pages.interact()

    async def _check_modlog_channel(self, ctx, channel_id,  message=None, *, embed=None):
        if not channel_id:
            message = (
                'Mod-logging should have a channel. '
                f'To set one, use `{ctx.clean_prefix}modlog channel`.\n\n'
                + (message or '')
            )

        await ctx.send(message, embed=embed)

    async def _show_config(self, ctx):
        config = await self._get_case_config(ctx.guild.id, connection=ctx.db)
        if not config:
            return await ctx.send(
                "Mod-logging hasn't been configured yet. "
                f"To turn on mod-logging, use `{ctx.clean_prefix}{ctx.invoked_with} channel`"
            )

        will, colour = ('will', 0x4CAF50) if config.enabled else ("won't", 0xF44336)
        flags = ', '.join(f.name for f in ActionFlag if config.events & f)

        count = await _get_number_of_cases(ctx.db, ctx.guild.id)
        embed = (discord.Embed(colour=colour, description=f'I have made {count} cases so far!')
                 .set_author(name=f'In {ctx.guild}, I {will} be logging mod actions.')
                 .add_field(name='Logging Channel', value=f'<#{config.channel_id}>')
                 .add_field(name='Actions that will be logged', value=flags, inline=False)
                 )
        await ctx.send(embed=embed)

    @commands.group(invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def modlog(self, ctx, enable: bool = None):
        """Sets whether or not I should log moderation actions at all.

        If no arguments are given, I'll show the basic configuration info.
        """

        if enable is None:
            return await self._show_config(ctx)

        query = """INSERT INTO modlog_config (guild_id, enabled) VALUES ($1, $2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET enabled = $2
                   RETURNING channel_id;
                """
        channel_id, = await ctx.db.fetchrow(query, ctx.guild.id, enable)

        message = ("Yay! What are the mods gonna do today? ^o^"
                   if enable else
                   "Ok... back to the corner I go... :c")
        await self._check_modlog_channel(ctx, channel_id, message)

    @modlog.command(name='channel')
    @commands.has_permissions(manage_guild=True)
    async def modlog_channel(self, ctx, channel: discord.TextChannel):
        """Sets the channel that will be used for logging moderation actions"""
        permissions = ctx.me.permissions_in(channel)
        if not permissions.read_messages:
            return await ctx.send(
                f'I need to be able to read messages in {channel.mention} you baka!'
            )

        if not permissions.send_messages:
            return await ctx.send(
                f'I need to be able to send messages in {channel.mention}. '
                'How else will I be able to log?!'
            )

        if not permissions.embed_links:
            return await ctx.send(
                'I need the Embed Links permissions in order to make '
                f'{channel.mention} the mod-log channel...'
            )

        query = """INSERT INTO modlog_config (guild_id, channel_id) VALUES ($1, $2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET channel_id = $2
                """
        await ctx.db.execute(query, ctx.guild.id, channel.id)

        await ctx.send('Ok, {channel.mention} it is then!')

    @commands.group(name='modactions', aliases=['modacts'], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def mod_actions(self, ctx):
        """Shows all the actions that can be logged.

        For this command to work, you have to make sure that you've
        set a channel for logging cases first.
        """
        config = await self._get_config(ctx)

        flags = ', '.join(f.name for f in ActionFlag)
        enabled_flags = ', '.join(f.name for f in ActionFlag if config.events & f)

        embed = (discord.Embed(colour=ctx.bot.colour)
                 .add_field(name='List of valid Mod Actions', value=flags)
                 .add_field(name='Actions that will be logged', value=enabled_flags)
                 )

        await self._check_modlog_channel(ctx, config['channel_id'], embed=embed)

    async def _set_actions(self, ctx, query, flags, *, colour):
        flags = unique(flags)
        reduced = reduce(operator.or_, flags)
        channel_id, events = await ctx.db.fetchrow(query, ctx.guild.id, reduced)

        enabled_flags = ', '.join(f.name for f in ActionFlag if events & f)

        embed = (discord.Embed(colour=colour, description=', '.join(f.name for f in flags))
                 .set_author(name=f'Successfully {ctx.command.name}d the following actions')
                 .add_field(name='The following mod actions will now be logged',
                            value=enabled_flags, inline=False)
                 )

        await self._check_modlog_channel(ctx, channel_id, embed=embed)

    @mod_actions.command(name='enable')
    @commands.has_permissions(manage_guild=True)
    async def macts_enable(self, ctx, *actions: ActionFlag):
        """Enables case creation for all the given mod-actions."""

        # The duplicated query is to prevent potential SQL injections.
        query = """INSERT INTO modlog_config (guild_id, enabled) VALUES ($1, DEFAULT | $2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET events = events | $2
                   RETURNING channel_id, events;
                """

        await self._set_actions(ctx, query, actions, colour=0x4CAF50)

    @mod_actions.command(name='disable')
    @commands.has_permissions(manage_guild=True)
    async def macts_disable(self, ctx, *actions: ActionFlag):
        """Disables case creation for all the given mod-actions."""

        # The duplicated query is to prevent potential SQL injections.
        query = """INSERT INTO modlog_config (guild_id, events) VALUES ($1, DEFAULT & ~$2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET events = events & ~$2
                   RETURNING channel_id, events;
        """

        await self._set_actions(ctx, query, actions, colour=0xF44336)

    @commands.command(name='pollauditlog')
    @commands.has_permissions(manage_guild=True)
    async def poll_audit_log_command(self, ctx, enable: bool):
        """Sets whether or not I should poll the audit log for certain cases.

        When you invoke a moderation command, e.g. `{prefix}ban`,
        it will be automatically logged on the given mod-log channel.

        This is meant for times when it's manually done (e.g. a manual
        ban or kick), or when it's done through another bot.

        Note that this is implicitly disabled if the bot cannot see the
        audit logs. However, this is still preferred, as the bot needs
        to see the audit logs for other commands (eg `{prefix}info role`)

        For this command to work, you have to make sure that you've
        set a channel for logging cases first.
        """
        query = """INSERT INTO modlog_config (guild_id, poll_audit_log) VALUES ($1, $2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET poll_audit_log = $2
                   RETURNING channel_id;
                """

        channel_id, = await ctx.db.fetchrow(query, ctx.guild.id, enable)

        message = '\U0001f440' if enable else '\U0001f626'
        await self._check_modlog_channel(ctx, channel_id, message)

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def moddm(self, ctx, dm_user: bool):
        """Sets whether or not I should DM the user
        when a mod-action is applied on them.

        (e.g. getting warned, kicked, muted, etc.)
        """
        query = """INSERT INTO modlog_config (guild_id, dm_user) VALUES ($1, $2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET dm_user = $2
                   RETURNING channel_id;
                """

        channel_id, = await ctx.db.fetchrow(query, ctx.guild.id, dm_user)
        await self._check_modlog_channel(ctx, channel_id, '\N{OK HAND SIGN}')

    # XXX: This command takes *way* too long.
    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def reason(self, ctx, num: CaseNumber, *, reason):
        """Sets the reason for a particular case.

        You must own this case in order to edit the reason.

        Negative numbers are allowed. They count starting from
        the most recent case. e.g. -1 will show the newest case,
        and -10 will show the 10th newest case.
        """

        # this reason command will kill me.
        case = await self._get_case(ctx.guild.id, num, connection=ctx.db)
        if case is None:
            return await ctx.send(f"Case #{num} doesn't exist.")

        if case['mod_id'] != ctx.author.id:
            return await ctx.send("This case is not yours.")

        channel = ctx.guild.get_channel(case['channel_id'])
        if not channel:
            return await ctx.send('This channel no longer exists... :frowning:')

        message = await _get_message(channel, case['message_id'])
        if not message:
            return await ctx.send('Somehow this message was deleted...')

        embed = message.embeds[0]
        reason_field = embed.fields[-1]
        embed.set_field_at(-1, name=reason_field.name, value=reason, inline=False)

        try:
            await message.edit(embed=embed)
        except discord.NotFound:
            # In case the message was cached, and the message was deleted
            # While it was still in the cache.
            return await ctx.send('Somehow this message was deleted...')

        query = 'UPDATE modlog SET reason = $1 WHERE id = $2;'
        await ctx.db.execute(query, reason, case['id'])
        await ctx.send('\N{OK HAND SIGN}')


def setup(bot):
    bot.add_cog(ModLog(bot))
