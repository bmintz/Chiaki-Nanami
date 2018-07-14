import collections
import discord
import datetime
import itertools
import math
import psutil
import random
import traceback

from discord.ext import commands
from functools import partial
from more_itertools import all_equal, ilen

from ..utils import db
from ..utils.formats import pluralize
from ..utils.misc import emoji_url
from ..utils.paginator import Paginator, FieldPaginator
from ..utils.time import human_timedelta

from core import errors


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

_celebration = partial(random.choices, '\U0001f38a\U0001f389', k=8)

class Stats:
    def __init__(self, bot):
        self.bot = bot
        self.process = psutil.Process()

    @commands.command(name='stats')
    @commands.bot_has_permissions(embed_links=True)
    async def stats(self, ctx):
        """Shows some general statistics about the bot.

        Do not confuse this with `{prefix}about` which is just the
        general info. This is just numbers.
        """

        bot = self.bot

        with self.process.oneshot():
            memory_usage_in_mb = self.process.memory_full_info().uss / 1024**2
            cpu_usage = self.process.cpu_percent() / psutil.cpu_count()

        uptime_seconds = bot.uptime.total_seconds()

        presence = (
            f'{bot.guild_count} Servers\n'
            f'{ilen(bot.get_all_channels())} Channels\n'
            f'{bot.user_count} Users'
        )

        chiaki_embed = (discord.Embed(description=bot.appinfo.description, colour=self.bot.colour)
                        .set_author(name=str(ctx.bot.user), icon_url=bot.user.avatar_url)
                        .add_field(name='CPU Usage', value=f'{cpu_usage}%\n{memory_usage_in_mb :.2f}MB')
                        .add_field(name='Presence', value=presence)
                        .add_field(name='Uptime', value=self.bot.str_uptime.replace(', ', '\n'))
                        )
        await ctx.send(embed=chiaki_embed)

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
    bot.add_cog(Stats(bot))
