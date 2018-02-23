import collections
import discord
import datetime
import itertools
import math
import psutil
import traceback

from discord.ext import commands
from more_itertools import ilen, partition

from ..utils.formats import pluralize
from ..utils.misc import emoji_url
from ..utils.paginator import ListPaginator, EmbedFieldPages
from ..utils.time import human_timedelta

from core import errors
from core.cog import Cog

__schema__ = """
    CREATE TABLE IF NOT EXISTS commands (
        id BIGSERIAL PRIMARY KEY NOT NULL,
        guild_id BIGINT NULL,
        channel_id BIGINT NOT NULL,
        author_id BIGINT NOT NULL,
        used TIMESTAMP NOT NULL,
        prefix TEXT NOT NULL,
        command TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS commands_author_id_idx ON commands (author_id);
    CREATE INDEX IF NOT EXISTS commands_command_idx ON commands (command);
    CREATE INDEX IF NOT EXISTS commands_guild_id_idx ON commands (guild_id);
"""

_ignored_exceptions = (
    commands.NoPrivateMessage,
    commands.DisabledCommand,
    commands.CheckFailure,
    commands.CommandNotFound,
    commands.UserInputError,
    discord.Forbidden,
    errors.ChiakiException,
)

ERROR_ICON_URL = emoji_url('\N{NO ENTRY SIGN}')


class Stats(Cog):
    def __init__(self, bot):
        self.bot = bot
        self.process = psutil.Process()

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
        await ListPaginator(ctx, lines, title=f'Top {title}').interact()

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
        extension_stats = '\n'.join(f'{len(set(getattr(bot, attr).values()))} {attr}'
                                    for attr in ('cogs', 'extensions'))

        with self.process.oneshot():
            memory_usage_in_mb = self.process.memory_full_info().uss / 1024**2
            cpu_usage = self.process.cpu_percent() / psutil.cpu_count()

        uptime_seconds = bot.uptime.total_seconds()
        average_messages = bot.message_counter / uptime_seconds
        message_field = f'{bot.message_counter} messages\n({average_messages :.2f} messages/sec)'

        text, voice = partition(lambda c: isinstance(c, discord.TextChannel), bot.get_all_channels())
        presence = (f"{bot.guild_count} Servers\n{ilen(text)} Text Channels\n"
                    f"{ilen(voice)} Voice Channels\n{bot.user_count} Users")

        chiaki_embed = (discord.Embed(description=bot.appinfo.description, colour=self.bot.colour)
                        .set_author(name=str(ctx.bot.user), icon_url=bot.user.avatar_url)
                        .add_field(name='Modules', value=extension_stats)
                        .add_field(name='CPU Usage', value=f'{cpu_usage}%\n{memory_usage_in_mb :.2f}MB')
                        .add_field(name='Messages', value=message_field)
                        .add_field(name='Presence', value=presence)
                        .add_field(name='Commands', value=command_stats)
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
        pages = EmbedFieldPages(ctx, lines, title=f"{ctx.author}'s last {title}",
                                inline=False, lines_per_page=5)
        await pages.interact()

    async def command_stats(self):
        pass

    async def shard_stats(self, ctx):
        """Shows the status for each of my shards, assuming I support sharding."""
        if not hasattr(ctx.bot, 'shards'):
            return await ctx.send("I don't support shards... yet.")
        # TODO

    async def on_command_error(self, ctx, error):
        # command_counter['failed'] += 0 sets the 'failed' key. We don't want that.
        if not isinstance(error, commands.CommandNotFound):
            self.bot.command_counter['failed'] += 1

        error = getattr(error, 'original', error)

        if isinstance(error, _ignored_exceptions):
            return

        e = (discord.Embed(colour=0xcc3366)
             .set_author(name=f'Error in command {ctx.command}', icon_url=ERROR_ICON_URL)
             .add_field(name='Author', value=f'{ctx.author}\n(ID: {ctx.author.id})', inline=False)
             .add_field(name='Channel', value=f'{ctx.channel}\n(ID: {ctx.channel.id})')
             )

        if ctx.guild:
            e.add_field(name='Guild', value=f'{ctx.guild}\n(ID: {ctx.guild.id})')

        exc = ''.join(traceback.format_exception(type(error), error, error.__traceback__, chain=False))
        e.description = f'```py\n{exc}\n```'
        e.timestamp = datetime.datetime.utcnow()
        await self.bot.webhook.send(embed=e)

    def _is_bot_farm(self, guild):
        checker = self.bot.get_cog('AntiBotCollections')
        if checker is None:
            return False

        return checker.is_bot_farm(guild)

    async def send_guild_stats(self, guild, colour, header, *, check_bot_farm=True):
        bots = sum(m.bot for m in guild.members)
        total = guild.member_count
        online = sum(m.status is discord.Status.online for m in guild.members)

        e = (discord.Embed(colour=colour)
             .set_author(name=f'{header} server.')
             .add_field(name='Name', value=guild.name)
             .add_field(name='ID', value=guild.id)
             .add_field(name='Owner', value=f'{guild.owner} (ID: {guild.owner.id})')
             .add_field(name='Members', value=str(total))
             .add_field(name='Bots', value=f'{bots} ({bots/total :.2%})')
             .add_field(name='Online', value=f'{online} ({online/total :.2%})')
             )

        if guild.icon:
            e.set_thumbnail(url=guild.icon_url)

        if guild.me:
            e.timestamp = guild.me.joined_at

        if check_bot_farm and self._is_bot_farm(guild):
            e.description = '\N{WARNING SIGN} This server **might** be a bot collection server.'

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
