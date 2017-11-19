import asyncpg
import datetime
import discord

from discord.ext import commands

from .utils import disambiguate
from .utils.misc import emoji_url, truncate

from core.cog import Cog


_blocked_icon = emoji_url('\N{NO ENTRY}')
_unblocked_icon = emoji_url('\N{WHITE HEAVY CHECK MARK}')


class Blacklisted(commands.CheckFailure):
    def __init__(self, message, reason, *args):
        self.message = message
        self.reason = reason
        super().__init__(message, *args)

    def as_embed(self):
        embed = (discord.Embed(colour=0xFF0000, vdescription=self.reason)
                 .set_author(name=self.message, icon_url=_blocked_icon)
                 )

        if self.reason:
            embed.description = self.reason

        return embed


_GuildOrUser = disambiguate.union(discord.Guild, discord.User)


class Blacklists(Cog, hidden=True):
    def __init__(self, bot):
        self.bot = bot

    async def __local_check(self, ctx):
        return await ctx.bot.is_owner(ctx.author)

    async def __global_check_once(self, ctx):
        async def get_blacklist(id):
            query = "SELECT reason FROM blacklist WHERE snowflake = $1;"
            return await ctx.db.fetchrow(query, id)

        row = await get_blacklist(ctx.author.id)
        if row:
            raise Blacklisted('You have been blacklisted by the owner.', row['reason'])

        # Only check if it's in DM after checking the user to prevent users
        # from attempting to bypass the blacklist through DM
        if ctx.guild is None:
            return True

        row = await get_blacklist(ctx.guild.id)
        if row:
            raise Blacklisted('This server has been blacklisted by the owner.', row['reason'])

        return True

    # Not sure if I should show the error or not.
    async def on_command_error(self, ctx, error):
        if isinstance(error, Blacklisted):
            await ctx.send(embed=error.as_embed())

    async def _show_blacklist_embed(self, ctx, colour, action, icon, thing, reason, time):
        embed = discord.Embed(colour=colour)
        type_name = 'Server' if isinstance(thing, discord.Guild) else 'User'
        reason = truncate(reason, 1024, '...') if reason else 'None'

        embed = (discord.Embed(colour=colour, timestamp=time)
                 .set_author(name=f'{type_name} {action}', icon_url=icon)
                 .add_field(name='Name', value=thing)
                 .add_field(name='ID', value=thing.id)
                 .add_field(name='Reason', value=reason, inline=False)
                 )

        await ctx.send(embed=embed)

    @commands.command(aliases=['bl'])
    @commands.is_owner()
    async def blacklist(self, ctx, server_or_user: _GuildOrUser, *, reason=''):
        """Blacklists either a server or a user, from using the bot."""

        if await ctx.bot.is_owner(server_or_user):
            return await ctx.send("You can't blacklist my sensei you baka...")

        time = datetime.datetime.utcnow()
        query = "INSERT INTO blacklist (snowflake, blacklisted_at, reason) VALUES ($1, $2, $3);"

        try:
            await ctx.db.execute(query, server_or_user.id, time, reason)
        except asyncpg.UniqueViolationError:
            return await ctx.send(f'{server_or_user} has already been blacklisted.')
        else:
            await self._show_blacklist_embed(ctx, 0xd50000, 'blacklisted', _blocked_icon,
                                             server_or_user, reason, time)

    @commands.command(aliases=['ubl'])
    @commands.is_owner()
    async def unblacklist(self, ctx, server_or_user: _GuildOrUser, *, reason=''):
        """Removes either a server or a user from the blacklist."""

        if await ctx.bot.is_owner(server_or_user):
            return await ctx.send("You can't blacklist my sensei you baka...")

        query = "DELETE FROM blacklist WHERE snowflake = $1;"
        result = await ctx.db.execute(query, server_or_user.id)
        if result[-1] == '0':
            return await ctx.send(f"{server_or_user} isn't blacklisted.")

        await self._show_blacklist_embed(ctx, 0x4CAF50, 'unblacklisted', _unblocked_icon,
                                         server_or_user, reason, datetime.datetime.utcnow())


def setup(bot):
    bot.add_cog(Blacklists(bot))
