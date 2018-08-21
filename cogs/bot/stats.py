import collections
import itertools
import math
import random
from functools import partial
from operator import attrgetter

import discord
import psutil
from discord.ext import commands
from more_itertools import all_equal, chunked, ilen

from ..utils import db
from ..utils.formats import pluralize
from ..utils.paginator import FieldPaginator, Paginator
from ..utils.time import human_timedelta


class Commands(db.Table):
    id = db.Column(db.BigSerial, primary_key=True)
    guild_id = db.Column(db.BigInt, nullable=True)
    channel_id = db.Column(db.BigInt)
    author_id = db.Column(db.BigInt)
    used = db.Column(db.Timestamp)
    prefix = db.Column(db.Text)
    command = db.Column(db.Text)

    commands_author_id_idx = db.Index(author_id)
    commands_command_idx = db.Index(command)
    commands_guild_id_idx = db.Index(guild_id)


_celebration = partial(random.choices, '\U0001f38a\U0001f389', k=8)


SHARD_STATE_EMOJIS = {
    'CONNECTING': '[CONNECTING]',
    'OPEN':       '[ONLINE]',
    'CLOSING':    '[DISCONNECTING]',
    'CLOSED':     '[OFFLINE]',
}
SHARD_STATE_COLOURS = {
    'CONNECTING': 0xFFC107,
    'OPEN': 0x4CAF50,
    'CLOSING': 0x424242,
    'CLOSED':  0xF44336,
}

SHARDS_PER_PAGE = 20
class ShardPaginator(Paginator):
    # These are all pointless
    first = last = goto = None

class Stats:
    def __init__(self, bot):
        self.bot = bot
        self.process = psutil.Process()
        if getattr(bot, 'shard_count', None) is None:
            self.shards = None  # "Remove" the command as its unavailable

    async def on_command(self, ctx):
        command = ctx.command.qualified_name
        self.bot.command_leaderboard[command] += 1

        guild_id = None if ctx.guild is None else ctx.guild.id

        query = """INSERT INTO commands (guild_id, channel_id, author_id, used, prefix, command)
                   VALUES ($1, $2, $3, $4, $5, $6)
                """

        await ctx.pool.execute(
            query,
            guild_id,
            ctx.channel.id,
            ctx.author.id,
            ctx.message.created_at,
            ctx.prefix,
            command,
        )

    async def _show_top_commands(self, ctx, n, entries):
        padding = int(math.log10(n)) + 1
        lines = (f'`\u200b{i:>{padding}}.`  {c} ({pluralize(use=u)})'
                 for i, (c, u) in enumerate(entries, 1))

        title = pluralize(command=n)
        await Paginator(ctx, lines, title=f'Top {title}').interact()

    @commands.group(name='topcommands', aliases=['topcmds'], invoke_without_command=True)
    async def top_commands(self, ctx, n=10):
        """Shows the n most used commands since I've woken up."""
        entries = self.bot.command_leaderboard.most_common(n)
        await self._show_top_commands(ctx, n, entries)

    @top_commands.group(name='alltime', aliases=['all'])
    async def top_commands_alltime(self, ctx, n=10):
        """Shows the top n commands of all time, globally."""
        query = """SELECT command,
                          COUNT(*) as "uses"
                   FROM commands
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT $1;
                """
        results = await ctx.db.fetch(query, n)
        await self._show_top_commands(ctx, n, results)

    @top_commands.group(name='alltimeserver', aliases=['allserver'])
    async def top_commands_alltimeserver(self, ctx, n=10):
        """Shows the top n commands of all time, in the server."""
        query = """SELECT command,
                          COUNT(*) as "uses"
                   FROM commands
                   WHERE guild_id = $1
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT $2;
                """
        results = await ctx.db.fetch(query, ctx.guild.id, n)
        await self._show_top_commands(ctx, n, results)

    @commands.command(name='stats')
    @commands.bot_has_permissions(embed_links=True)
    async def stats(self, ctx):
        """Shows some general statistics about the bot.

        Do not confuse this with `{prefix}about` which is just the
        general info. This is just numbers.
        """

        bot = self.bot
        command_map = itertools.starmap('{1} {0}'.format, bot.command_counter.most_common())
        command_stats = '\n'.join(command_map) or 'No stats yet.'
        commands = f'{len(bot.commands)}\n({len(set(bot.walk_commands()))} total)'

        with self.process.oneshot():
            memory_usage_in_mb = self.process.memory_full_info().uss / 1024**2
            cpu_usage = self.process.cpu_percent() / psutil.cpu_count()

        uptime_seconds = bot.uptime.total_seconds()
        average_messages = bot.message_counter / uptime_seconds
        message_field = f'{bot.message_counter}\n({average_messages :.2f}/sec)'

        presence = (
            f'{bot.guild_count} Servers\n'
            f'{ilen(bot.get_all_channels())} Channels\n'
            f'{bot.user_count} Users'
        )

        chiaki_embed = (discord.Embed(description=bot.appinfo.description, colour=self.bot.colour)
                        .set_author(name=str(ctx.bot.user), icon_url=bot.user.avatar_url)
                        .add_field(name='Commands', value=commands)
                        .add_field(name='CPU Usage', value=f'{cpu_usage}%\n{memory_usage_in_mb :.2f}MB')
                        .add_field(name='Messages', value=message_field)
                        .add_field(name='Presence', value=presence)
                        .add_field(name='Commands Run', value=command_stats)
                        .add_field(name='Uptime', value=self.bot.str_uptime.replace(', ', '\n'))
                        )
        await ctx.send(embed=chiaki_embed)

    @commands.command()
    async def history(self, ctx, n=5):
        """Shows the last n commands you've used."""
        n = min(n, 50)

        query = """SELECT prefix, command, used FROM commands
                   WHERE author_id = $1
                   ORDER BY id DESC
                   OFFSET 1 -- skip this command
                   LIMIT $2;
                """
        lines = [
            (f'`{prefix}{command}`', f'Executed {human_timedelta(used)}')
            for prefix, command, used in await ctx.db.fetch(query, ctx.author.id, n)
        ]

        title = pluralize(command=n)
        pages = FieldPaginator(ctx, lines, title=f"{ctx.author}'s last {title}",
                                inline=False, per_page=5)
        await pages.interact()

    async def command_stats(self):
        pass

    @commands.command()
    async def shards(self, ctx):
        """Shows the status for all the shards"""
        shard_guild_counts = collections.Counter(g.shard_id for g in ctx.bot.guildsview())
        shards = sorted(ctx.bot.shards.values(), key=attrgetter('id'))

        def default_shard_state_names():
            names = (SHARD_STATE_EMOJIS[shard.ws.state.name] for shard in shards)
            for chunk in chunked(names, SHARDS_PER_PAGE):
                max_width = max(map(len, chunk))
                for name in chunk:
                    fill = " \u200b" * (max_width - len(name) + 1)
                    yield f'`{fill}{name}`'

        def shard_states():
            if ctx.bot_has_permissions(external_emojis=True):
                emoji_keys = ['connecting', 'online', 'disconnecting', 'offline']
                e_config = ctx.bot.emoji_config
                emojis = [getattr(e_config, 'shard_' + attr) for attr in emoji_keys]
                if all(emojis):
                    e_dict = dict(zip(SHARD_STATE_EMOJIS, emojis))
                    return (e_dict[shard.ws.state.name] for shard in shards)

            return default_shard_state_names()

        lines = (
             f'{status} Shard **#{shard.id}** \u2014 **{shard_guild_counts[shard.id]}** servers'
             for status, shard in zip(shard_states(), shards)
        )
        shard_id = ctx.guild.shard_id if ctx.guild else 0
        shard = ctx.bot.shards[shard_id]

        paginator = ShardPaginator(
            ctx,
            lines,
            title=f"Shards (currently on Shard #{shard_id})",
            colour=SHARD_STATE_COLOURS[shard.ws.state.name],
            per_page=SHARDS_PER_PAGE,
        )
        await paginator.run()

    def _is_bot_farm(self, guild):
        checker = self.bot.get_cog('AntiBotCollections')
        if checker is None:
            return False

        return checker.is_bot_farm(guild)

    @staticmethod
    def _is_guild_count_landmark(guild_count):
        """Return True if the bot is in a special number of guilds"""
        # TODO: Put this in config.py
        guild_count_string = str(guild_count)
        return (
            # 1111, 22222, 55555, etc.
            all_equal(guild_count_string)
            # 2000, 30000, 40000, etc
            or (guild_count_string[0] != '1' and set(guild_count_string[1:]) == {'0'})
            or Stats._is_guild_count_milestone(guild_count - 1)
            or Stats._is_guild_count_milestone(guild_count + 1)
        )

    @staticmethod
    def _is_guild_count_milestone(guild_count):
        """Return True if the bot is in a *really* special number of guilds"""
        # TODO: Put this in config.py
        guild_count_string = str(guild_count)
        return guild_count_string[0] in {'1', '5'} and set(guild_count_string[1:]) == {'0'}

    async def send_guild_stats(self, guild, colour, header, *, check_bot_farm=True):
        bots = sum(m.bot for m in guild.members)
        total = guild.member_count
        online = sum(m.status is discord.Status.online for m in guild.members)

        guild_count = self.bot.guild_count
        guild_count_message = f'Now in **{guild_count}** servers!'

        if self._is_guild_count_milestone(guild_count):
            message = f'\N{BIRTHDAY CAKE} {guild_count_message}!! \N{BIRTHDAY CAKE}'
            guild_count_message = f'{"".join(_celebration())}\n{message}\n{"".join(_celebration())}'
        elif self._is_guild_count_landmark(guild_count):
            guild_count_message = f'\N{PARTY POPPER} {guild_count_message}!! \N{PARTY POPPER}'

        info = (
            f'{guild_count_message}\n\u200b\n'
            f'\N{NAME BADGE} **Name**: {guild.name}\n'
            f'\N{SQUARED ID} **ID**: {guild.id}\n'
            f'\N{CROWN} **Owner**: {guild.owner} ({guild.owner.mention})\n'
            f'\u2022 **{total}** Members \u2022 **{bots}** Bots \u2022 **{online}** Online ({online/total :.2%})\n'
        )

        e = discord.Embed(colour=colour, description=info)
        e.set_author(name=f'{header} server')

        if guild.icon:
            e.set_thumbnail(url=guild.icon_url)

        if guild.me:
            e.timestamp = guild.me.joined_at

        if check_bot_farm and self._is_bot_farm(guild):
            e.colour = 0xFFC107
            e.description += f'\n\N{WARNING SIGN} **Might** be a bot collection server.'

        await self.bot.webhook.send(embed=e)

    async def on_guild_join(self, guild):
        await self.send_guild_stats(guild, 0x53dda4, 'New',)

    async def on_guild_remove(self, guild):
        # No need to check if Chiaki left a bot collection server lol.
        await self.send_guild_stats(guild, 0xdd5f53, 'Left', check_bot_farm=False)


def setup(bot):
    if not hasattr(bot, 'command_leaderboard'):
        bot.command_leaderboard = collections.Counter()
    bot.add_cog(Stats(bot))
