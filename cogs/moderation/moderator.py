import asyncio
import contextlib
import datetime
import functools
import heapq
import itertools
import random
import typing
from collections import Counter, namedtuple
from operator import attrgetter

import discord
from discord.ext import commands

from ..utils import db, formats, time, varpos
from ..utils.context_managers import temp_attr
from ..utils.examples import get_example, static_example, wrap_example
from ..utils.jsonf import JSONFile
from ..utils.misc import ordinal
from ..utils.paginator import FieldPaginator, Paginator


class WarnEntries(db.Table, table_name='warn_entries'):
    id = db.Column(db.Serial, primary_key=True)
    guild_id = db.Column(db.BigInt)
    user_id = db.Column(db.BigInt)
    mod_id = db.Column(db.BigInt)
    reason = db.Column(db.Text)
    warned_at = db.Column(db.Timestamp)

class WarnTimeouts(db.Table, table_name='warn_timeouts'):
    guild_id = db.Column(db.BigInt, primary_key=True)
    timeout = db.Column(db.Interval)

class WarnPunishments(db.Table, table_name='warn_punishments'):
    guild_id = db.Column(db.BigInt)
    warns = db.Column(db.BigInt)
    text = db.Column(db.Text)
    duration = db.Column(db.Integer, default=0)
    __create_extra__ = ['PRIMARY KEY(guild_id, warns)']

class MutedRoles(db.Table, table_name='muted_roles'):
    guild_id = db.Column(db.BigInt, primary_key=True)
    role_id = db.Column(db.BigInt)


class AlreadyWarned(commands.CommandError):
    """Exception raised to avoid the case where a failed-warn due
    to the cooldown would be considered to be a success"""
    __ignore__ = True

class AlreadyMuted(commands.CommandError):
    """Exception raised to avoid muting a member that's already muted being
    considered "successful" """
    __ignore__ = True


# Dummy punishment class for default warn punishment
_DummyPunishment = namedtuple('_DummyPunishment', 'warns type duration')
_default_punishment = _DummyPunishment(warns=3, type='mute', duration=60 * 10)
del _DummyPunishment


def _get_lower_member(ctx):
    member = random.choice([
        member for member in ctx.guild.members
        if ctx.author.id != member.id
        and member.id != ctx.bot.user.id
        and member != ctx.guild.owner
        and ctx.author.top_role > member.top_role < ctx.me.top_role
    ] or ctx.guild.members)
    return f'@{member}'


class _ProxyMember:
    def __init__(self, id):
        self.id = id

    def __str__(self):
        return str(self.id)

class MemberID(commands.Converter):
    async def convert(self, ctx, arg):
        try:
            return await commands.MemberConverter().convert(ctx, arg)
        except commands.BadArgument:
            pass

        try:
            id = int(arg)
        except ValueError:
            raise commands.BadArgument(f"{arg} is not a member or ID.") from None
        else:
            return _ProxyMember(id)

    @staticmethod
    def random_example(ctx):
        if random.random() > 0.5:
            return _get_lower_member(ctx)

        exists = ctx.guild.get_member
        user_ids = [u.id for u in ctx.bot.users]
        user_ids = list(itertools.filterfalse(exists, user_ids)) or user_ids
        return random.choice(user_ids)


class BannedMember(commands.Converter):
    async def convert(self, ctx, arg):
        ban_list = await ctx.guild.bans()
        try:
            member_id = int(arg, base=10)
        except ValueError:
            thing = discord.utils.find(lambda e: str(e.user) == arg, ban_list)
        else:
            thing = discord.utils.find(lambda e: e.user.id == member_id, ban_list)

        if thing is None:
            raise commands.BadArgument(f"{arg} wasn't previously-banned in this server...")
        return thing

    @staticmethod
    def random_example(ctx):
        # Querying guild.bans requires an API request and is overkill
        # for this.
        return 'SomeBannedUser#0000'


class _CheckedMember(commands.Converter):
    def __init__(self, type=commands.MemberConverter):
        self.converter = type()

    async def convert(self, ctx, arg):
        member = await self.converter.convert(ctx, arg)

        # e.g. ->ban returning a discord.Object in order to ban someone
        # not in the server
        if not isinstance(member, discord.Member):
            return member

        if ctx.author.id == member.id:
            raise commands.BadArgument("Please don't hurt yourself. :(")
        if member.id == ctx.bot.user.id:
            raise commands.BadArgument("Hey, what did I do??")
        if member == ctx.guild.owner:
            raise commands.BadArgument(f"Hey hey, don't try to {ctx.command} the server owner!")
        if member.top_role >= ctx.me.top_role:
            if ctx.author != ctx.guild.owner and member.top_role >= ctx.author.top_role:
                extra = 'the both of us'
            else:
                extra = 'me'
            raise commands.BadArgument(f"{member} is higher than {extra}.")
        if member.top_role >= ctx.author.top_role:
            raise commands.BadArgument(f"{member} is higher than you.")

        return member

    def random_example(self, ctx):
        # Can't duck-type this cuz we only want users we can actually ban
        if self.converter is commands.MemberConverter:
            return _get_lower_member(ctx)
        return get_example(self.converter, ctx)

CheckedMember = _CheckedMember()
CheckedMemberID = _CheckedMember(MemberID)

@static_example
class Reason(commands.Converter):
    async def convert(self, ctx, arg):
        result = f'{ctx.author} \N{EM DASH} {arg}'

        if len(result) > 512:
            max_ = 512 - len(result) - len(arg)
            raise commands.BadArgument(
                f'Maximum reason length is {max_} characters (got {len(arg)})'
            )

        return result


_warn_punishments = ['mute', 'kick', 'softban', 'tempban', 'ban']
_punishment_needs_duration = {'mute', 'tempban'}.__contains__
_is_valid_punishment = frozenset(_warn_punishments).__contains__

def warn_punishment(arg):
    # I guess we could use partition here but meh muh quotes
    view = commands.view.StringView(arg)
    punishment = commands.view.quoted_word(view)
    lowered = punishment.lower()

    if not _is_valid_punishment(lowered):
        raise commands.BadArgument(
            f'{punishment} is not a valid punishment.\n'
            f'Valid punishments: {", ".join(_warn_punishments)}'
        )

    if not _punishment_needs_duration(lowered):
        return lowered, None

    view.skip_ws()
    duration = commands.view.quoted_word(view)
    if not duration:
        raise commands.BadArgument(f'A duration is required for {punishment}...')

    duration = time.Delta(duration)
    return lowered, duration

@wrap_example(warn_punishment)
def _warn_punishment_example(ctx):
    punishment = random.choice(_warn_punishments)
    if not _punishment_needs_duration(punishment):
        return punishment

    duration = time.Delta.random_example(ctx)
    return f'{punishment} {duration}'

num_warns = functools.partial(int)
@wrap_example(num_warns)
def _num_warns_example(_):
    return random.randint(3, 5)


# TODO:
# - implement anti-raid protocol
# - implement antispam
# - implement mention-spam
class Moderator:
    def __init__(self, bot):
        self.bot = bot

        self.slowmodes = JSONFile('slowmodes.json')
        self.slowmode_bucket = {}

        if hasattr(self.bot, '__mod_mute_role_create_bucket__'):
            self._mute_role_create_cooldowns = self.bot.__mod_mute_role_create_bucket__
        else:
            self._mute_role_create_cooldowns = commands.CooldownMapping.from_cooldown(
                rate=2, per=600, type=commands.BucketType.guild
            )

    def __unload(self):
        self.bot.__mod_mute_role_create_bucket__ = self._mute_role_create_cooldowns

    async def call_mod_log_invoke(self, invoke, ctx):
        mod_log = ctx.bot.get_cog('ModLog')
        if mod_log:
            await getattr(mod_log, f'mod_{invoke}')(ctx)

    __before_invoke = functools.partialmethod(call_mod_log_invoke, 'before_invoke')
    __after_invoke = functools.partialmethod(call_mod_log_invoke, 'after_invoke')

    # ---------------- Slowmode ------------------

    @staticmethod
    def _is_slowmode_immune(member):
        return member.guild_permissions.manage_guild

    async def check_slowmode(self, message):
        if message.guild is None:
            return

        guild_id = message.guild.id
        if guild_id not in self.slowmodes:
            return

        slowmodes = self.slowmodes[guild_id]

        author = message.author
        is_immune = self._is_slowmode_immune(author)

        for thing in (message.channel, author):
            key = str(thing.id)
            if key not in slowmodes:
                continue

            config = slowmodes[key]
            if not config['no_immune'] and is_immune:
                continue

            bucket = self.slowmode_bucket.setdefault(thing.id, {})
            time = bucket.get(author.id)
            if time is None or (message.created_at - time).total_seconds() >= config['duration']:
                bucket[author.id] = message.created_at
            else:
                await message.delete()
                break

    @commands.group(invoke_without_command=True)
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def slowmode(self, ctx, duration: time.Delta, *, member: discord.Member = None):
        """Puts a thing in slowmode.

        An optional member argument can be provided. If it's
        given, it puts only that user in slowmode for the entire
        server. Otherwise it puts the current channel in slowmode.

        Those with Manage Server permissions will not be
        affected. If you want to put them in slowmode too,
        use `{prefix}slowmode noimmune`.
        """
        pronoun = 'They'
        if member is None:
            member = ctx.channel
            pronoun = 'Everyone'
        elif self._is_slowmode_immune(member):
            message = (
                f'{member} is immune from slowmode due to having the '
                f'Manage Server permission. Consider using `{ctx.prefix}slowmode '
                'no-immune` or giving them a harsher punishment.'
            )

            return await ctx.send(message)

        config = self.slowmodes.get(ctx.guild.id, {})
        slowmode = config.setdefault(str(member.id), {'no_immune': False})
        if slowmode['no_immune']:
            return await ctx.send(
                f'{member.mention} is already in **no-immune** slowmode. '
                'You need to turn it off first.'
            )

        slowmode['duration'] = duration.duration
        await self.slowmodes.put(ctx.guild.id, config)

        await ctx.send(
            f'{member.mention} is now in slowmode! '
            f'{pronoun} must wait {duration} '
            'between each message they send.'
        )

    @slowmode.command(name='noimmune', aliases=['n-i'])
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def slowmode_no_immune(self, ctx, duration: time.Delta, *, member: discord.Member = None):
        """Puts the channel or member in "no-immune" slowmode.

        Unlike `{prefix}slowmode`, no one is immune to this slowmode,
        even those with Manage Server permissions, which means everyone's messages
        will be deleted if they are within the duration given.
        """
        if member is None:
            member, pronoun = ctx.channel, 'They'
        else:
            pronoun = 'Everyone'

        config = self.slowmodes.get(ctx.guild.id, {})
        slowmode = config.setdefault(str(member.id), {'no_immune': True})
        slowmode['duration'] = duration.duration
        await self.slowmodes.put(ctx.guild, config)

        await ctx.send(f'{member.mention} is now in **no-immune** slowmode! '
                       f'{pronoun} must wait {duration} '
                       'after each message they send.')

    @slowmode.command(name='off')
    async def slowmode_off(self, ctx, *, member: discord.Member = None):
        """Turns off slowmode for either a member or channel."""
        member = member or ctx.channel
        config = self.slowmodes.get(ctx.guild.id, {})
        try:
            del config[str(member.id)]
        except KeyError:
            return await ctx.send(f'{member.mention} was never in slowmode... \N{NEUTRAL FACE}')
        else:
            await self.slowmodes.put(ctx.guild.id, config)
            self.slowmode_bucket.pop(member.id, None)
            await ctx.send(f'{member.mention} is no longer in slowmode... '
                           '\N{SMILING FACE WITH OPEN MOUTH AND COLD SWEAT}')

    @commands.command()
    @commands.has_permissions(manage_messages=True)
    async def slowoff(self, ctx, *, member: discord.Member = None):
        """Alias for `{prefix}slowmode off`"""
        await ctx.invoke(self.slowmode_off, member=member)

    # ----------------------- End slowmode ---------------------

    @commands.command(aliases=['newmembers', 'joined'])
    @commands.guild_only()
    async def newusers(self, ctx, *, count=5):
        """Tells you the newest members of the server.

        This is useful to check if any suspicious members have joined.

        The minimum is 3 members. If no number is given I'll show the last 5 members.
        """
        human_delta = time.human_timedelta
        count = max(count, 3)
        members = heapq.nlargest(count, ctx.guild.members, key=attrgetter('joined_at'))

        names = map(str, members)
        values = (
            (f'**Joined:** {human_delta(member.joined_at)}\n'
             f'**Created:** {human_delta(member.created_at)}\n{"-" * 40}')
            for member in members
        )
        entries = zip(names, values)

        title = f'The {formats.pluralize(**{"newest members": len(members)})}'
        pages = FieldPaginator(ctx, entries, per_page=5, colour=0x00FF00, title=title)
        await pages.interact()

    @commands.command(aliases=['clr'])
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def clear(self, ctx, num_or_user: typing.Union[int, discord.Member] = None):
        """Clears some messages in a channels

        The argument can either be a user or a number.
        If it's a number it deletes *up to* that many messages.
        If it's a user, it deletes any message by that user up to the last 100 messages.
        If no argument was specified, it deletes my messages.
        """

        if isinstance(num_or_user, int):
            if num_or_user < 1:
                return await ctx.send(f"How can I delete {num_or_user} messages...?")
            deleted = await ctx.channel.purge(limit=min(num_or_user, 1000) + 1)
        elif isinstance(num_or_user, discord.Member):
            deleted = await ctx.channel.purge(check=lambda m: m.author.id == num_or_user.id)
        else:
            deleted = await ctx.channel.purge(check=lambda m: m.author.id == ctx.bot.user.id)

        messages = formats.pluralize(message=len(deleted) - 1)
        await ctx.send(f"Deleted {messages} successfully!", delete_after=1.5)

    @commands.command(aliases=['clean'])
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cleanup(self, ctx, limit=100):
        """Cleans up my messages from the channel.

        If I have the Manage Messages and Read Message History perms, I can also
        try to delete messages that look like they invoked my commands.

        When I'm done cleaning up. I will show the stats of whose messages got deleted
        and how many. This should give you an idea as to who are spamming me.

        You can also use this if `{prefix}clear` fails.
        """

        prefixes = tuple(ctx.bot.get_guild_prefixes(ctx.guild))
        bot_id = ctx.bot.user.id

        bot_perms = ctx.channel.permissions_for(ctx.me)
        purge = functools.partial(ctx.channel.purge, limit=limit, before=ctx.message)
        can_bulk_delete = bot_perms.manage_messages and bot_perms.read_message_history

        if can_bulk_delete:
            def is_possible_command_invoke(m):
                if m.author.id == bot_id:
                    return True
                return m.content.startswith(prefixes) and not m.content[1:2].isspace()
            deleted = await purge(check=is_possible_command_invoke)
        else:
            # We can only delete the bot's messages, because trying to delete
            # other users' messages without Manage Messages will raise an error.
            # Also we can't use bulk-deleting for the same reason.
            deleted = await purge(check=lambda m: m.author.id == bot_id, bulk=False)
        spammers = Counter(str(m.author) for m in deleted)

        total_deleted = sum(spammers.values())
        second_part = ' was' if total_deleted == 1 else 's were'
        title = f'{total_deleted} message{second_part} removed.'

        joined = '\n'.join(itertools.starmap('**{0}**: {1}'.format, spammers.most_common()))

        if ctx.bot_has_embed_links():
            spammer_stats = joined or discord.Embed.Empty

            embed = (discord.Embed(colour=0x00FF00, description=spammer_stats)
                     .set_author(name=title)
                     )
            embed.timestamp = ctx.message.created_at

            await ctx.send(embed=embed, delete_after=20)
        else:
            message = f'{title}\n{joined}'
            await ctx.send(message, delete_after=20)

        await asyncio.sleep(20)
        with contextlib.suppress(discord.HTTPException):
            await ctx.message.delete()

    @clear.error
    @cleanup.error
    async def clear_error(self, ctx, error):
        # We need to use the __cause__ because any non-CommandErrors will be
        # wrapped in CommandInvokeError
        cause = error.__cause__
        if not isinstance(cause, discord.HTTPException):
            ctx.__bypass_local_error__ = True
            return

        await ctx.send(
            "Couldn't delete the messages for some reason... Here's the error:\n"
            f"```py\n{type(cause).__name__}: {cause}```"
        )

    async def _get_warn_timeout(self, connection, guild_id):
        query = 'SELECT timeout FROM warn_timeouts WHERE guild_id = $1;'
        row = await connection.fetchrow(query, guild_id)
        return row['timeout'] if row else datetime.timedelta(minutes=15)

    @commands.command()
    @commands.has_permissions(manage_messages=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str):
        """Warns a user (obviously)"""
        author, current_time, guild_id = ctx.author, ctx.message.created_at, ctx.guild.id
        timeout = await self._get_warn_timeout(ctx.db, guild_id)

        query = """SELECT warned_at FROM warn_entries
                   WHERE guild_id = $1 AND user_id = $2 AND warned_at + $3 > $4
                   ORDER BY id
                """
        records = await ctx.db.fetch(query, guild_id, member.id, timeout, current_time)
        warn_queue = [r[0] for r in records]

        try:
            last_warn = warn_queue[-1]
        except IndexError:
            pass
        else:
            retry_after = (current_time - last_warn).total_seconds()
            if retry_after <= 60:
                # Must throw an error because return await triggers on_command_completion
                # Which would end up logging a case even though it doesn't work.
                raise AlreadyWarned(f"{member} has been warned already, try again in "
                                    f"{60 - retry_after :.2f} seconds...")

        # Add the warn
        query = """INSERT INTO warn_entries (guild_id, user_id, mod_id, reason, warned_at)
                   VALUES ($1, $2, $3, $4, $5)
                """
        await ctx.db.execute(query, guild_id, member.id, author.id, reason, current_time)

        # See if there's a punishment
        current_warn_number = len(warn_queue) + 1
        query = 'SELECT type, duration FROM warn_punishments WHERE guild_id = $1 AND warns = $2'
        row = await ctx.db.fetchrow(query, guild_id, current_warn_number)

        if row is None:
            if current_warn_number == 3:
                row = _default_punishment
            else:
                return await ctx.send(f"\N{WARNING SIGN} Warned {member.mention} successfully!")

        # Auto-punish the user
        args = [member]
        duration = row['duration']
        if duration > 0:
            args.append(duration)
            punished_for = f' for {time.duration_units(duration)}'
        else:
            punished_for = f''

        punishment = row['type']
        punishment_command = getattr(self, punishment)
        punishment_reason = f'{reason}\n({ordinal(current_warn_number)} warning)'

        # Patch out the context's send method because we don't want it to be
        # sending the command's message.
        # XXX: Should I suppress the error?
        with temp_attr(ctx, 'send', lambda *a, **kw: asyncio.sleep(0)):
            await ctx.invoke(punishment_command, *args, reason=punishment_reason)

        message = (
            f"{member.mention} has {current_warn_number} warnings! "
            f"**It's punishment time!** Today I'll {punishment} you{punished_for}! "
            "\N{SMILING FACE WITH HORNS}"
        )
        await ctx.send(message)

        # Dynamically patch the attributes because case logging requires them.
        # If they weren't patched in, it would treat is as if it was a warn action.
        ctx.auto_punished = True
        ctx.command = punishment_command
        ctx.args[2:] = args
        ctx.kwargs['reason'] = punishment_reason

    @warn.error
    async def warn_error(self, ctx, error):
        if isinstance(error, AlreadyWarned):
            await ctx.send(error)
        else:
            ctx.__bypass_local_error__ = True

    # XXX: Should this be a group?

    @commands.command(name='clearwarns')
    @commands.has_permissions(manage_messages=True)
    async def clear_warns(self, ctx, member: discord.Member):
        """Clears a member's warns."""
        query = 'DELETE FROM warn_entries WHERE guild_id = $1 AND user_id = $2'
        await ctx.db.execute(query, ctx.guild.id, member.id)
        await ctx.send(f"{member}'s warns have been reset!")

    @commands.command(name='warnpunish')
    @commands.has_permissions(manage_messages=True, manage_guild=True)
    async def warn_punish(self, ctx, num: num_warns, *, punishment: warn_punishment):
        """Sets the punishment a user receives upon exceeding a given warn limit.

        Valid punishments are:
        `mute` (requires a duration argument)
        `kick`
        `softban`
        `tempban` (requires a duration argument)
        `ban`
        """
        punishment, duration = punishment
        true_duration = None if duration is None else duration.duration


        query = """INSERT INTO warn_punishments (guild_id, warns, type, duration)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (guild_id, warns)
                   DO UPDATE SET type = $3, duration = $4;
                """
        await ctx.db.execute(query, ctx.guild.id, num, punishment, true_duration)

        extra = f'for {duration}' if duration else ''
        await ctx.send(f'\N{OK HAND SIGN} if a user has been warned {num} times, '
                       f'I will **{punishment}** them {extra}.')

    @commands.command(name='warnpunishments', aliases=['warnpl'])
    async def warn_punishments(self, ctx):
        """Shows this list of warn punishments"""
        query = """SELECT warns, initcap(type), duration FROM warn_punishments
                   WHERE guild_id = $1
                   ORDER BY warns;
                """
        punishments = await ctx.db.fetch(query, ctx.guild.id) or (_default_punishment, )

        entries = (
            f'{warns} strikes => **{type}** {f"for {time.duration_units(duration)}" if duration else ""}'
            for warns, type, duration in punishments
        )

        pages = Paginator(ctx, entries, title=f'Punishments for {ctx.guild}')
        await pages.interact()

    @commands.command(name='warntimeout')
    @commands.has_permissions(manage_messages=True, manage_guild=True)
    async def warn_timeout(self, ctx, duration: time.Delta):
        """Sets the maximum time between the oldest warn and the most recent warn.
        If a user hits a warn limit within this timeframe, they will be punished.
        """
        query = """INSERT INTO warn_timeouts (guild_id, timeout) VALUES ($1, $2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET timeout = $2
                """
        await ctx.db.execute(query, ctx.guild.id, datetime.timedelta(seconds=duration.duration))

        await ctx.send(
            f'Alright, if a user was warned within **{duration}** '
            'after their oldest warn, bad things will happen.'
        )

    async def _get_muted_role_from_db(self, guild, *, connection=None):
        connection = connection or self.bot.pool

        query = 'SELECT role_id FROM muted_roles WHERE guild_id = $1'
        row = await connection.fetchrow(query, guild.id)
        if row is None:
            return None

        return discord.utils.get(guild.roles, id=row['role_id'])

    async def _get_muted_role(self, guild, connection=None):
        role = await self._get_muted_role_from_db(guild, connection=connection)
        if role is not None:
            return role

        def probably_mute_role(r):
            lowered = r.name.lower()
            return lowered == 'muted' or 'mute' in lowered

        return discord.utils.find(probably_mute_role, reversed(guild.roles))

    async def _update_muted_role(self, guild, new_role, connection=None):
        connection = connection or self.bot.pool
        query = """INSERT INTO muted_roles (guild_id, role_id) VALUES ($1, $2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET role_id = $2
                """
        await connection.execute(query, guild.id, new_role.id)

    @staticmethod
    async def _regen_muted_role_perms(role, *channels):
        muted_permissions = dict.fromkeys(['send_messages', 'manage_messages', 'add_reactions',
                                           'speak', 'connect', 'use_voice_activation'], False)

        permissions_in = channels[0].guild.me.permissions_in
        for channel in channels:
            if not permissions_in(channel).manage_roles:
                # Save discord the HTTP request
                continue

            await asyncio.sleep(random.uniform(0, 0.5))

            try:
                await channel.set_permissions(role, **muted_permissions)
            except discord.NotFound as e:
                # The role could've been deleted midway while Chiaki was
                # setting up the overwrites.
                if 'Unknown Overwrite' in str(e):
                    raise
            except discord.HTTPException:
                pass

    async def _do_mute(self, member, when, role, *, connection=None, reason=None):
        if role in member.roles:
            raise AlreadyMuted(f'{member.mention} is already been muted... ;-;')

        await member.add_roles(role, reason=reason)

        if when is not None:
            args = (member.guild.id, member.id, role.id)
            await self.bot.db_scheduler.add_abs(when, 'mute_complete', args)

    async def _create_muted_role(self, ctx):
        # Needs to be released as the process of creating a new role
        # and creating the overwrites can take a hell of a long time
        await ctx.release()

        bucket = self._mute_role_create_cooldowns.get_bucket(ctx.message)
        if not bucket.get_tokens():
            retry_after = bucket.update_rate_limit() or 0  # e d g e c a s e s
            raise commands.CommandOnCooldown(bucket, retry_after)

        if not await ctx.ask_confirmation('No muted role found. Create a new one?', delete_after=False):
            await ctx.send(
                "A muted role couldn't be found. "
                f'Set one with `{ctx.clean_prefix}setmuterole Role`'
            )
            return None

        bucket.update_rate_limit()
        async with ctx.typing():
            ctx.__new_mute_role_message__ = await ctx.send('Creating muted role. Please wait...')
            role = await ctx.guild.create_role(
                name='Chiaki-Muted',
                colour=discord.Colour.red(),
                reason='Creating new muted role'
            )

            with contextlib.suppress(discord.HTTPException):
                await role.edit(position=ctx.me.top_role.position - 1)

            await self._regen_muted_role_perms(role, *ctx.guild.channels)
            await ctx.acquire()
            await self._update_muted_role(ctx.guild, role, ctx.db)
            return role

    @commands.command()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def mute(self, ctx, member: CheckedMember, duration: typing.Optional[time.Delta]=None, *, reason: Reason = None):
        """Mutes a user for an optional amount of time (obviously)"""
        reason = reason or f'By {ctx.author}'

        async def try_edit(content):
            try:
                await ctx.__new_mute_role_message__.edit(content=content)
            except (AttributeError, discord.NotFound):
                await ctx.send(content)

        role = await self._get_muted_role(ctx.guild, ctx.db)
        if role is None:
            try:
                role = await self._create_muted_role(ctx)
            except discord.NotFound:
                return await try_edit("Please don't delete the role while I'm setting it up.")
            except asyncio.TimeoutError:
                return await ctx.send('Sorry. You took too long...')
            except commands.CommandOnCooldown as e:
                return await ctx.send(
                    "You're deleting the muted role too much.\n"
                    f'Please wait {time.duration_units(e.retry_after)} before trying again, '
                    f'or set a muted role with `{ctx.clean_prefix}setmuterole Role`'
                )
            if role is None:
                return

        if duration is None:
            when = None
            for_how_long = f'permanently'
        else:
            when = ctx.message.created_at + duration.delta
            for_how_long = f'for {duration}'

        await self._do_mute(member, when, role, connection=ctx.db, reason=reason)
        await try_edit(f'Done. {member.mention} will now be muted {for_how_long}. \N{ZIPPER-MOUTH FACE}')

    @mute.error
    async def mute_error(self, ctx, error):
        if isinstance(error, AlreadyMuted):
            await ctx.send(error)
        else:
            ctx.__bypass_local_error__ = True

    @commands.command()
    async def mutetime(self, ctx, member: discord.Member = None):
        """Shows the time left for a member's mute. Defaults to yourself."""
        if member is None:
            member = ctx.author

        # early out for the case of premature role removal,
        # either by ->unmute or manually removing the role
        role = await self._get_muted_role(ctx.guild, ctx.db)
        if role not in member.roles:
            return await ctx.send(f'{member} is not muted...')

        query = """SELECT expires
                   FROM schedule
                   WHERE event = 'mute_complete'
                   AND args_kwargs #>> '{args,0}' = $1
                   AND args_kwargs #>> '{args,1}' = $2

                   -- The below condition is in case we have this scenario:
                   -- - Member was muted
                   -- - Mute role was changed while the user was muted
                   -- - Member was muted again with the new role.
                   AND args_kwargs #>> '{args,2}' = $3
                   LIMIT 1;
                """

        entry = await ctx.db.fetchrow(query, str(ctx.guild.id), str(member.id), str(role.id))
        if entry is None:
            return await ctx.send(f"{member} has been perm-muted, you must've "
                                  "added the role manually or something...")

        when = entry['expires']
        await ctx.send(f'{member} has {time.human_timedelta(when)} remaining. '
                       f'They will be unmuted on {when: %c}.')

    async def _remove_time_entry(self, guild, member, connection=None, *, event='mute_complete'):
        connection = connection or self.bot.pool
        query = """SELECT id, expires
                   FROM schedule
                   WHERE event = $3
                   AND args_kwargs #>> '{args,0}' = $1
                   AND args_kwargs #>> '{args,1}' = $2
                   ORDER BY expires
                   LIMIT 1;
                """
        entry = await connection.fetchrow(query, str(guild.id), str(member.id), event)
        if entry is None:
            return None

        await self.bot.db_scheduler.remove(discord.Object(id=entry['id']))
        return entry['expires']

    @commands.command()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(self, ctx, member: discord.Member, *, reason: Reason = None):
        """Unmutes a user (obviously)"""
        reason = reason or f'Unmute by {ctx.author}'

        role = await self._get_muted_role(member.guild, ctx.db)
        if role not in member.roles:
            return await ctx.send(f"{member} hasn't been muted!")

        await member.remove_roles(role, reason=reason)
        await self._remove_time_entry(member.guild, member, ctx.db)
        await ctx.send(f'{member.mention} can now speak again... '
                       '\N{SMILING FACE WITH OPEN MOUTH AND COLD SWEAT}')

    @commands.command(name='setmuterole', aliases=['muterole', 'smur'])
    @commands.has_permissions(manage_roles=True, manage_guild=True)
    async def set_muted_role(self, ctx, *, role: discord.Role):
        """Sets the muted role for the server."""
        await self._update_muted_role(ctx.guild, role, ctx.db)
        await ctx.send(f'Set the muted role to **{role}**!')

    @commands.command()
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: CheckedMember, *, reason: Reason = None):
        """Kick a user (obviously)"""
        reason = reason or f'By {ctx.author}'

        await member.kick(reason=reason)
        await ctx.send("Done. Please don't make me do that again...")

    @commands.command(aliases=['sb'])
    @commands.has_permissions(kick_members=True, manage_messages=True)
    @commands.bot_has_permissions(ban_members=True)
    async def softban(self, ctx, member: CheckedMember, *, reason: Reason = None):
        """Softbans a user (obviously)"""
        reason = reason or f'By {ctx.author}'

        await member.ban(reason=reason)
        await member.unban(reason=f'softban (original reason: {reason})')
        await ctx.send("Done. At least he'll be ok...")

    @commands.command(aliases=['tb'])
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def tempban(self, ctx, member: CheckedMember, duration: time.Delta, *, reason: Reason = None):
        """Temporarily bans a user (obviously)"""
        reason = reason or f'By {ctx.author}'

        await ctx.guild.ban(member, reason=reason)
        await ctx.send("Done. Please don't make me do that again...")

        await ctx.bot.db_scheduler.add(duration.delta, 'tempban_complete', (ctx.guild.id, member.id))

    @commands.command()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx, member: CheckedMemberID, *, reason: Reason = None):
        """Bans a user (obviously)

        You can also use this to ban someone even if they're not in the server,
        just use the ID. (not so obviously)
        """
        reason = reason or f'By {ctx.author}'
        await ctx.guild.ban(member, reason=reason)
        await ctx.send("Done. Please don't make me do that again...")

    @commands.command()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, user: BannedMember, *, reason: Reason = None):
        """Unbans the user (obviously)"""
        reason = reason or f'By {ctx.author}'

        await ctx.guild.unban(user.user, reason=reason)
        await self._remove_time_entry(ctx.guild, user.user, ctx.db, event='tempban_complete')
        await ctx.send(f"Done. What did {user.user} do to get banned in the first place...?")

    @varpos.require_va_command()
    @commands.has_permissions(ban_members=True)
    async def massban(self, ctx, reason: Reason, *members: CheckedMemberID):
        """Bans multiple users from the server (obviously)"""
        for m in members:
            await ctx.guild.ban(m, reason=reason)

        await ctx.send(f"Done. What happened...?")

    # --------- Events ---------

    async def on_message(self, message):
        await self.check_slowmode(message)

    async def on_guild_channel_create(self, channel):
        server = channel.guild

        # Don't bother creating a mute role if there isn't one set, because
        # people might just want to create a channel without having to deal
        # with moderation commands. Only when people want to use the actual
        # mute command should we create the muted role if there isn't one.
        role = await self._get_muted_role_from_db(server)
        if role is None:
            return

        await self._regen_muted_role_perms(role, channel)

    async def on_member_join(self, member):
        # Prevent mute-evasion
        expires = await self._remove_time_entry(member.guild, member)
        if not expires:
            return

        role = await self._get_muted_role(member.guild)
        if not role:
            return

        # Mute them for an extra 60 mins. There might be some edge case
        # where a member leaves the server and the muted role gets set to
        # a different role but I'm far too lazy to figure that edge case out.
        await self._do_mute(
            member,
            expires + datetime.timedelta(seconds=3600),
            role,
            # TODO: Perhaps I should put the new date in the reason?
            reason='Mute Evasion'
        )

    async def on_member_update(self, before, after):
        # In the event of a manual unmute, this has to be covered.
        removed_roles = set(before.roles).difference(after.roles)
        if not removed_roles:
            return  # Give an early out to save queries.

        role = await self._get_muted_role(before.guild)
        if role in removed_roles:
            # We need to remove this guy from the scheduler in the event of
            # a manual unmute. Because if the guy was muted again, the old
            # mute would still be in effect. So it would just remove the
            # muted role.
            await self._remove_time_entry(before.guild, before)

    # XXX: Should I even bother to remove unbans from the scheduler in the event
    #      of a manual unban?

    # -------- Custom Events (used in schedulers) -----------
    async def _wait_for_cache(self, name, guild_id, member_id):
        mod_log = self.bot.get_cog('ModLog')
        if mod_log:
            await mod_log.wait_for_cache(name, guild_id, member_id)

    async def on_mute_complete(self, timer):
        server_id, member_id, mute_role_id = timer.args
        server = self.bot.get_guild(server_id)
        if server is None:
            # rip
            return

        member = server.get_member(member_id)
        if member is None:
            # rip pt. 2
            return

        role = discord.utils.get(server.roles, id=mute_role_id)
        if role is None:
            # not really rip
            return

        await member.remove_roles(role)

    async def on_tempban_complete(self, timer):
        guild_id, user_id = timer.args
        await self._wait_for_cache('tempban', guild_id, user_id)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # rip
            return

        await guild.unban(discord.Object(id=user_id), reason='unban from tempban')


def setup(bot):
    bot.add_cog(Moderator(bot))
