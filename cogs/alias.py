import copy

from discord.ext import commands
from itertools import starmap

from .utils.paginator import ListPaginator

from core.cog import Cog

__schema__ = """
    CREATE TABLE IF NOT EXISTS command_aliases (
        id SERIAL PRIMARY KEY,
        guild_id BIGINT NOT NULL,
        alias TEXT NOT NULL,
        command TEXT NOT NULL
    );

    CREATE UNIQUE INDEX IF NOT EXISTS command_aliases_uniq_idx
    ON command_aliases (guild_id, alias);
"""

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

        query = """INSERT INTO command_aliases (guild_id, alias, command)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (guild_id, alias)
                   DO UPDATE SET command = $3;
                """
        await ctx.db.execute(query, ctx.guild.id, alias, command)
        await ctx.send(f'Ok, typing "{ctx.prefix}{alias}" will now be '
                       f'the same as "{ctx.prefix}{command}"')

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def delalias(self, ctx, *, alias):
        """Deletes an alias."""
        query = 'DELETE FROM command_aliases WHERE guild_id = $1 AND alias = $2;'
        await ctx.db.execute(query, ctx.guild.id, alias)
        await ctx.send(f'Ok... bye "{alias}"')

    @commands.command()
    async def aliases(self, ctx):
        """Shows all the aliases for the server"""
        query = """SELECT alias, command FROM command_aliases
                   WHERE guild_id = $1
                   ORDER BY alias;
                """
        entries = starmap('`{0}` => `{1}`'.format, await ctx.db.fetch(query, ctx.guild.id))
        pages = ListPaginator(ctx, entries)
        await pages.interact()

    async def _get_alias(self, guild_id, content, *, connection=None):
        connection = connection or self.bot.pool
        query = """SELECT alias, command FROM command_aliases
                   WHERE guild_id = $1
                   AND ($2 ILIKE alias || ' %' OR $2 = alias)
                   ORDER BY length(alias)
                   LIMIT 1;
                """
        return await connection.fetchrow(query, guild_id, content)

    def _get_prefix(self, message):
        prefixes = self.bot.get_guild_prefixes(message.guild)
        return next(filter(message.content.startswith, prefixes), None)

    async def on_message(self, message):
        prefix = self._get_prefix(message)
        if not prefix:
            return
        len_prefix = len(prefix)

        row = await self._get_alias(message.guild.id, message.content[len_prefix:])
        if row is None:
            return

        alias, command = row

        new_message = copy.copy(message)
        args = message.content[len_prefix + len(alias):]
        new_message.content = f"{prefix}{command}{args}"

        await self.bot.process_commands(new_message)


def setup(bot):
    bot.add_cog(Aliases(bot))
