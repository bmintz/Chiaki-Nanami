import aiohttp
import asyncqlio
import discord
import datetime
import itertools
import math
import psutil
import traceback

from discord.ext import commands
from more_itertools import ilen, partition

from .utils import errors
from .utils.formats import pluralize
from .utils.misc import emoji_url
from .utils.paginator import ListPaginator, EmbedFieldPages
from .utils.time import human_timedelta

from core.cog import Cog

_Table = asyncqlio.table_base()
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


class Command(_Table, table_name='commands'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)
    guild_id = asyncqlio.Column(asyncqlio.BigInt, index=True, nullable=True)
    commands_guild_id_idx = asyncqlio.Index(guild_id)

    channel_id = asyncqlio.Column(asyncqlio.BigInt)
    author_id = asyncqlio.Column(asyncqlio.BigInt, index=True)
    commands_author_id_idx = asyncqlio.Index(author_id)

    used = asyncqlio.Column(asyncqlio.Timestamp)
    prefix = asyncqlio.Column(asyncqlio.String)
    command = asyncqlio.Column(asyncqlio.String, index=True)
    commands_command_idx = asyncqlio.Index(command)


class Stats(Cog):
    def __init__(self, bot):
        self.bot = bot
        self._md = self.bot.db.bind_tables(_Table)
        self.process = psutil.Process()

    async def on_command(self, ctx):
        command = ctx.command.qualified_name
        self.bot.command_leaderboard[command] += 1

        guild_id = None if ctx.guild is None else ctx.guild.id
        row = Command(
            guild_id=guild_id,
            channel_id=ctx.channel.id,
            author_id=ctx.author.id,
            used=ctx.message.created_at,
            prefix=ctx.prefix,
            command=command,
        )

        async with ctx.db.get_session() as s:
            await s.add(row)

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
                   LIMIT {n};
                """
        results = await (await ctx.session.cursor(query, {'n': n})).flatten()
        await self._show_top_commands(ctx, n, (r.values() for r in results))

    @top_commands.group(name='alltimeserver', aliases=['allserver'])
    async def top_commands_alltimeserver(self, ctx, n=10):
        """Shows the top n commands of all time, in the server."""
        query = """SELECT command,
                          COUNT(*) as "uses"
                   FROM commands
                   WHERE guild_id = {guild_id}
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT {n};
                """
        params = {'n': n, 'guild_id': ctx.guild.id}
        results = await (await ctx.session.cursor(query, params)).flatten()
        print(results)
        await self._show_top_commands(ctx, n, (tuple(r.values()) for r in results))

    @commands.command(name='stats')
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
        presence = (f"{len(bot.guilds)} Servers\n{ilen(text)} Text Channels\n"
                    f"{ilen(voice)} Voice Channels\n{len(bot.users)} Users")

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

        query = (ctx.session.select.from_(Command)
                            .where(Command.author_id == ctx.author.id)
                            .order_by(Command.used, sort_order='desc')
                            .offset(1)  # Skip this command.
                            .limit(n)
                 )

        lines = [(f'`{row.prefix}{row.command}`', f'Executed {human_timedelta(row.used)}')
                 async for row in await query.all()]
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

    @commands.command(name='testerr')
    @commands.is_owner()
    async def test_error(self, ctx):
        """Tests the error logger webhook."""
        NO

    async def send_guild_stats(self, guild, colour, header):
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

        await self.bot.webhook.send(embed=e)

    async def on_guild_join(self, guild):
        await self.send_guild_stats(guild, 0x53dda4, 'New')

    async def on_guild_remove(self, guild):
        await self.send_guild_stats(guild, 0xdd5f53, 'Left')


def setup(bot):
    bot.add_cog(Stats(bot))
