import asyncqlio
import copy

from discord.ext import commands

from .tables.base import TableBase
from .utils.paginator import ListPaginator

from core.cog import Cog


class Alias(TableBase, table_name='command_aliases'):
    id = asyncqlio.Column(asyncqlio.Serial, primary_key=True)

    guild_id = asyncqlio.Column(asyncqlio.BigInt, index=True)
    alias = asyncqlio.Column(asyncqlio.String(2000), index=True)
    command = asyncqlio.Column(asyncqlio.String(2000))

    alias_idx = asyncqlio.Index(guild_id, alias, unique=True)


def _first_word(string):
    return string.split(' ', 1)[0]


def _first_word_is_command(group, string):
    return _first_word(string) in group.all_commands


class AliasName(commands.Converter):
    async def convert(self, ctx, arg):
        lowered = arg.lower().strip()
        if not lowered:
            raise commands.BadArgument('Actually type something please... -.-')

        if _first_word_is_command(ctx.bot, lowered):
            message = "You can't have a command as an alias. Don't be that cruel!"
            raise commands.BadArgument(message)

        return lowered


class Aliases(Cog):
    def __init__(self, bot):
        self.bot = bot

    # idk if this should be in a command group...
    #
    # I have it not in a command group to make things easier. This might seem weird
    # because the tag system is in a group. But I did this because retrieving a tag
    # is done by [p]tag <your tag>...

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def alias(self, ctx, alias: AliasName, *, command):
        """Creates an alias for a certain command.

        Aliases are case insensitive.

        If the alias already exists, using this command will
        overwrite the alias' command. Use `{prefix}delalias`
        if you want to remove the alias.

        For multi-word aliases you must use quotes.
        """
        if not _first_word_is_command(ctx.bot, command):
            return await ctx.send(f"{command} isn't an actual command...")

        # asyncqlio's upsert is broken. It passes ALL the columns in the query,
        # including the SERIAL column. This makes it pass None (NULL in SQL) in
        # the id column, causing it to fail due to a NotNullViolationError.
        #
        # I'll submit a PR at some point. This comment is here to remind me
        # of that.
        query = """INSERT INTO command_aliases (guild_id, alias, command)
                   VALUES ({guild_id}, {alias}, {command})
                   ON CONFLICT (guild_id, alias)
                   DO UPDATE SET command = {command};
                """
        params = {'guild_id': ctx.guild.id, 'alias': alias, 'command': command}
        await ctx.session.execute(query, params)
        # row = Alias(guild_id=ctx.guild.id, alias=alias, command=command)
        # await ctx.session.insert.add_row(row).on_conflict(AliasCC).update(Alias.command)
        await ctx.send(f'Ok, typing "{ctx.prefix}{alias}" will now be '
                       f'the same as "{ctx.prefix}{command}"')

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def delalias(self, ctx, *, alias):
        """Deletes an alias."""
        await ctx.session.delete.table(Alias).where((Alias.guild_id == ctx.guild.id) & (Alias.alias == alias))
        await ctx.send(f'Ok... bye "{alias}"')

    @commands.command()
    async def aliases(self, ctx):
        """Shows all the aliases for the server"""
        query = ctx.session.select.from_(Alias).where(Alias.guild_id == ctx.guild.id)
        entries = [f'`{row.alias}` => `{row.command}`' async for row in await query.all()]
        pages = ListPaginator(ctx, entries)
        await pages.interact()

    async def _get_alias(self, guild_id, content):
        async with self.bot.db.get_session() as s:
            # Must use raw SQL because asyncqlio doesn't support string concatenation,
            # nor does it support binary ops with columns that aren't setters.
            query = """SELECT alias, command FROM command_aliases
                       WHERE guild_id = {guild_id}
                       AND ({content} ILIKE alias || ' %' OR {content} = alias)
                       ORDER BY length(alias)
                       LIMIT 1;
                    """
            params = {'guild_id': guild_id, 'content': content}
            return await s.fetch(query, params)

    def _get_prefix(self, message):
        prefixes = self.bot.get_guild_prefixes(message.guild)
        return next(filter(message.content.startswith, prefixes), None)

    async def on_message(self, message):
        prefix = self._get_prefix(message)
        if not prefix:
            return
        len_prefix = len(prefix)

        alias = await self._get_alias(message.guild.id, message.content[len_prefix:])
        if not alias:
            return

        new_message = copy.copy(message)
        args = message.content[len_prefix + len(alias['alias']):]
        new_message.content = f"{prefix}{alias['command']}{args}"
        await self.bot.process_commands(new_message)


def setup(bot):
    bot.add_cog(Aliases(bot))
