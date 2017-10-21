import asyncio
import asyncqlio
import collections
import contextlib
import discord
import enum
import json
import logging
import operator

from datetime import datetime, timedelta
from discord.ext import commands
from functools import reduce

from .utils import cache, errors
from .utils.misc import emoji_url, truncate, unique
from .utils.paginator import EmbedFieldPages
from .utils.time import duration_units

from core.cog import Cog


log = logging.getLogger(__name__)


class ModLogError(errors.ChiakiException):
    pass


_Table = asyncqlio.table_base()


class Case(_Table, table_name='modlog'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)
    channel_id = asyncqlio.Column(asyncqlio.BigInt)
    message_id = asyncqlio.Column(asyncqlio.BigInt)

    guild_id = asyncqlio.Column(asyncqlio.BigInt)
    modlog_guild_id_idx = asyncqlio.Index(guild_id)

    action = asyncqlio.Column(asyncqlio.String(16))
    mod_id = asyncqlio.Column(asyncqlio.BigInt)
    reason = asyncqlio.Column(asyncqlio.String(1024))

    # Can either be the duration, in the case of a mute or tempban,
    # or the role, in the case of a special role.
    extra = asyncqlio.Column(asyncqlio.Text, default='{}')


class CaseTarget(_Table, table_name='modlog_targets'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)
    entry_id = asyncqlio.Column(asyncqlio.Integer, foreign_key=asyncqlio.ForeignKey(Case.id))
    user_id = asyncqlio.Column(asyncqlio.BigInt)


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

MASSBAN_THUMBNAIL = emoji_url('\N{NO ENTRY}')


class ModLogConfig(_Table, table_name='modlog_config'):
    guild_id = asyncqlio.Column(asyncqlio.BigInt, primary_key=True)
    channel_id = asyncqlio.Column(asyncqlio.BigInt, default=0)

    # flags
    # XXX Should I store this as one integer and use bitwise?
    enabled = asyncqlio.Column(asyncqlio.Boolean, default=True)
    log_auto = asyncqlio.Column(asyncqlio.Boolean, default=True)
    dm_user = asyncqlio.Column(asyncqlio.Boolean, default=True)
    poll_audit_log = asyncqlio.Column(asyncqlio.Boolean, default=True)

    events = asyncqlio.Column(asyncqlio.Integer, default=_default_flags)


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
async def _get_number_of_cases(session, guild_id):
    query = "SELECT COUNT(*) FROM modlog WHERE guild_id={guild_id};"
    params = {'guild_id': guild_id}
    result = await session.cursor(query, params)
    row = await result.fetch_row()

    return row['count']


class CaseNumber(commands.Converter):
    async def convert(self, ctx, arg):
        try:
            num = int(arg)
        except ValueError:
            raise commands.BadArgument("This has to be an actual number... -.-")

        if num < 0:
            num += await _get_number_of_cases(ctx.session, ctx.guild.id) + 1
            if num < 0:
                # Consider it out of bounds, because accessing a negative
                # index is out of bounds anyway.
                raise commands.BadArgument("I think you're travelling a little "
                                           "too far in the past there...")
        return num


class ModLog(Cog):
    def __init__(self, bot):
        self.bot = bot
        self._md = bot.db.bind_tables(_Table)
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

    async def _get_case_config(self, session, guild_id):
        query = (session.select.from_(ModLogConfig)
                        .where(ModLogConfig.guild_id == guild_id)
                 )
        return await query.first()

    async def _send_case(self, session, config, action, server, mod, targets, reason,
                         extra=None, auto=False):
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

        # Get the case number, this is why the guild_id is indexed.
        count = await _get_number_of_cases(session, server.id)

        # Send the case like normal
        embed = self._create_embed(count + 1, action, mod, targets, reason, extra)

        try:
            message = await channel.send(embed=embed)
        except discord.Forbidden:
            raise ModLogError(f"I can't send messages to {channel.mention}. Check my privileges pls...")

        # Add the case to the DB, because mod-logging was successful!
        row = await session.add(Case(
            guild_id=server.id,
            channel_id=channel.id,
            message_id=message.id,

            action=action,
            mod_id=mod.id,
            reason=reason,
            extra=json.dumps({'args': [extra]})

        ))

        return row.id

    def _create_embed(self, number, action, mod, targets, reason, extra, time=None):
        time = time or datetime.utcnow()
        action = _mod_actions[action]

        avatar_url = targets[0].avatar_url if len(targets) == 1 else MASSBAN_THUMBNAIL
        bot_avatar = self.bot.user.avatar_url

        duration_string = f' for {duration_units(extra)}' if extra is not None else ''
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

    async def _insert_case(self, session, action, server, mod, targets, reason, extra, entry_id):
        # Because we've successfully added a new case by this point,
        # the number of cases is no longer accurate.
        _get_number_of_cases.invalidate(None, server.id)

        if len(targets) == 1:
            await session.add(CaseTarget(entry_id=entry_id, user_id=targets[0].id))
        else:
            columns = ('entry_id', 'user_id')
            to_insert = [(entry_id, t.id) for t in targets]
            conn = session.transaction.acquired_connection
            await conn.copy_records_to_table('modlog_targets', columns=columns, records=to_insert)

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

        # We have get the config outside the two functions because we use it twice.
        config = await self._get_case_config(ctx.session, ctx.guild.id)
        args = [ctx.session, config, name, ctx.guild, ctx.author, targets, reason, extra]

        # XXX: I'm not sure if I should DM the user before or *after* the
        #      action has been applied. I currently have it done after, because
        #      the target should only be DMed if the command was executed
        #      successfully, and we can't check if it worked before we do
        #      the thing.
        await self._notify_user(*args[1:])

        try:
            entry_id = await self._send_case(*args, auto=auto)
        except ModLogError as e:
            await ctx.send(f'{ctx.author.mention}, {e}')
        else:
            if entry_id:
                del args[1]  # remove the config from the args because we don't need it.
                await self._insert_case(*args, entry_id)

    async def _poll_audit_log(self, guild, user, *, action):
        if (action, guild.id, user.id) in self._cache:
            # Assume it was invoked by a command (only commands will put this in the cache).
            return

        with contextlib.suppress(AttributeError):  # in case guild.me is a User for some reason
            if not guild.me.guild_permissions.view_audit_log:
                # early out
                return

        async with self.bot.db.get_session() as session:
            config = await self._get_case_config(session, guild.id)

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
            async with self.bot.db.get_session() as session:
                args = (session, config, action, guild, entry.user, [entry.target], entry.reason)
                entry_id = await self._send_case(*args)
                if entry_id:
                    await self._insert_case(*args, entry_id)

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

    async def _get_case(self, session, guild_id, num):
        query = (session.select.from_(Case)
                        .where(Case.guild_id == guild_id)
                        .order_by(Case.id)
                        .offset(num - 1)
                        .limit(1)
                 )
        return await query.first()

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
        num = num or await _get_number_of_cases(ctx.session, ctx.guild.id)

        result = await self._get_case(ctx.session, ctx.guild.id, num)
        if result is None:
            return await ctx.send(f'Case #{num} is not a valid case.')

        t_query = ctx.session.select.from_(CaseTarget).where(CaseTarget.entry_id == result.id)
        targets = [ctx.bot.get_user(row.user_id) or f'<Unknown: {row.user_id}>'
                   async for row in await t_query.all()]

        extra = json.loads(result.extra)
        extra = extra['args'][0] if extra else None

        # Parse the cases accordingly
        embed = self._create_embed(
            num,
            result.action,
            ctx.bot.get_user(result.mod_id),
            targets,
            result.reason,
            extra,
            discord.utils.snowflake_time(result.message_id),
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
                   AND guild_id = {guild_id}
                   AND user_id = {user_id}
                   ORDER BY modlog.id
                """

        params = {'guild_id': ctx.guild.id, 'user_id': member.id}
        results = await ctx.session.cursor(query, params)

        get_time = discord.utils.snowflake_time
        get_user = ctx.bot.get_user

        entries = []
        async for row in results:
            action = _mod_actions[row['action']]
            name = f'{action.emoji} {action.repr.title()}'
            formatted = (
                f"**On:** {get_time(row['message_id']) :%x %X}\n"
                # Gotta use triple-quotes to keep the syntax happy.
                f"""**Moderator:** {get_user(row['mod_id']) or f'<Unknown ID: {row["mod_id"]}'}\n"""
                f"**Reason:** {truncate(row['reason'], 512, '...')}\n"
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

    async def _check_config(self, ctx):
        config = await self._get_case_config(ctx.session, ctx.guild.id)
        if not (config and config.channel_id):
            message = ("You haven't even enabled case-logging. Set a channel "
                       f"first using `{ctx.clean_prefix}modlog channel`.")
            raise ModLogError(message)

        return config

    async def _show_config(self, ctx, config):
        count = await _get_number_of_cases(ctx.session, ctx.guild.id)
        will, colour = ('will', 0x4CAF50) if config.enabled else ("won't", 0xF44336)
        flags = ', '.join(f.name for f in ActionFlag if config.events & f)

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

        config = await self._check_config(ctx)
        if enable is None:
            return await self._show_config(ctx, config)

        config.enabled = enable
        await ctx.session.add(config)

        message = ("Yay! What are the mods gonna do today? ^o^"
                   if enable else
                   "Ok... back to the corner I go... :c")
        await ctx.send(message)

    @modlog.command(name='channel')
    @commands.has_permissions(manage_guild=True)
    async def modlog_channel(self, ctx, channel: discord.TextChannel):
        """Sets the channel that will be used for logging moderation actions"""
        permissions = ctx.me.permissions_in(channel)
        if not permissions.read_messages:
            return await ctx.send(f'I need to be able to read messages in {channel.mention} you baka!')

        if not permissions.send_messages:
            return await ctx.send(f'I need to be able to send messages in {channel.mention}. '
                                  'How else will I be able to log?!')

        if not permissions.embed_links:
            return await ctx.send('I need the Embed Links permissions in order to make '
                                  f'{channel.mention} the mod-log channel...')

        config = await self._get_case_config(ctx.session, ctx.guild.id)
        config = config or ModLogConfig(guild_id=ctx.guild.id)
        config.channel_id = channel.id

        await ctx.session.add(config)
        await ctx.send('Ok, {channel.mention} it is then!')

    @commands.group(name='modactions', aliases=['modacts'], invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def mod_actions(self, ctx):
        """Shows all the actions that can be logged.

        For this command to work, you have to make sure that you've
        set a channel for logging cases first.
        """
        config = await self._check_config(ctx)

        flags = ', '.join(f.name for f in ActionFlag)
        enabled_flags = ', '.join(f.name for f in ActionFlag if config.events & f)

        embed = (discord.Embed(colour=ctx.bot.colour)
                 .add_field(name='List of valid Mod Actions', value=flags)
                 .add_field(name='Actions that will be logged', value=enabled_flags)
                 )
        await ctx.send(embed=embed)

    async def _set_actions(self, ctx, op, flags, *, colour):
        flags = unique(flags)

        config = await self._check_config(ctx)
        reduced = reduce(operator.or_, flags)
        config.events = op(config.events, reduced)

        await ctx.session.add(config)

        enabled_flags = ', '.join(f.name for f in ActionFlag if config.events & f)

        embed = (discord.Embed(colour=colour, description=', '.join(f.name for f in flags))
                 .set_author(name=f'Successfully {ctx.command.name}d the following actions')
                 .add_field(name='The following mod actions will now be logged',
                            value=enabled_flags, inline=False)
                 )

        await ctx.send(embed=embed)

    @mod_actions.command(name='enable')
    @commands.has_permissions(manage_guild=True)
    async def macts_enable(self, ctx, *actions: ActionFlag):
        """Enables case creation for all the given mod-actions.

        For this command to work, you have to make sure that you've
        set a channel for logging cases first.
        """
        await self._set_actions(ctx, operator.or_, actions, colour=0x4CAF50)

    @mod_actions.command(name='disable')
    @commands.has_permissions(manage_guild=True)
    async def macts_disable(self, ctx, *actions: ActionFlag):
        """Disables case creation for all the given mod-actions.

        For this command to work, you have to make sure that you've
        set a channel for logging cases first.
        """
        await self._set_actions(ctx, lambda ev, f: ev & ~f, actions, colour=0xF44336)

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
        config = await self._check_config(ctx)
        config.poll_audit_log = enable
        await ctx.session.add(config)

        message = '\U0001f440' if enable else '\U0001f626'
        await ctx.send(message)

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def moddm(self, ctx, dm_user: bool):
        """Sets whether or not I should DM the user
        when a mod-action is applied on them.

        (e.g. getting warned, kicked, muted, etc.)
        """
        config = await self._get_case_config(ctx.session, ctx.guild.id)
        config = config or ModLogConfig(guild_id=ctx.guild.id)
        config.dm_user = dm_user

        await ctx.session.add(config)
        await ctx.send('\N{OK HAND SIGN}')

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

        case = await self._get_case(ctx.session, ctx.guild.id, num)
        if case is None:
            return await ctx.send(f"Case #{num} doesn't exist.")

        if case.mod_id != ctx.author.id:
            return await ctx.send("This case is not yours.")

        channel = ctx.guild.get_channel(case.channel_id)
        if not channel:
            return await ctx.send('This channel no longer exists... :frowning:')

        message = await _get_message(channel, case.message_id)
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

        case.reason = reason
        await ctx.session.add(case)
        await ctx.send('\N{OK HAND SIGN}')


def setup(bot):
    bot.add_cog(ModLog(bot))
