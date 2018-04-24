import asyncpg
import discord
import functools
import itertools
import operator
import random

from collections import defaultdict, namedtuple
from discord.ext import commands
from more_itertools import iterate, partition

from ..utils import cache, formats, disambiguate
from ..utils.converter import BotCommand, BotCogConverter
from ..utils.misc import emoji_url, truncate, unique
from ..utils.paginator import ListPaginator

from core.cog import Cog

__schema__ = """
    CREATE TABLE IF NOT EXISTS permissions (
        id SERIAL PRIMARY KEY,
        guild_id BIGINT NOT NULL,
        snowflake BIGINT NULL,
        name TEXT NOT NULL,
        whitelist BOOLEAN NOT NULL,
        UNIQUE (snowflake, name)

    );
    CREATE INDEX IF NOT EXISTS permissions_guild_id_idx ON permissions (guild_id);

    CREATE TABLE IF NOT EXISTS plonks (
        guild_id BIGINT,
        entity_id BIGINT,
        PRIMARY KEY(guild_id, entity_id)
    );
    CREATE INDEX IF NOT EXISTS plonks_idx ON plonks (guild_id, entity_id);
"""

ALL_COMMANDS_KEY = '*'


class _PermissionFormattingMixin:
    def _get_header(self):
        if self.command:
            return f'Command **{self.command}** is'
        elif self.cog == ALL_COMMANDS_KEY:
            return 'All commands are'
        else:
            category, _, cog = self.cog.partition('/')
            if cog:
                return f'Module **{cog}** is'
            return f'Category **{category.title()}** is'


class PermissionDenied(_PermissionFormattingMixin, commands.CheckFailure):
    def __init__(self, message, *args):
        name, obj, *rest = args
        self.object = obj
        self.cog, _, self.command = _extract_from_node(name)

        super().__init__(message, *args)

    def __str__(self):
        return (f'{self._get_header()} disabled for the {_get_class_name(self.object).lower()} '
                f'"{self.object}".')


class InvalidPermission(_PermissionFormattingMixin, commands.CommandError):
    def __init__(self, message, *args):
        name, whitelisted, *rest = args
        self.whitelisted = whitelisted
        self.cog, _, self.command = _extract_from_node(name)

        super().__init__(message, *args)

    def __str__(self):
        message = {
            False: 'disabled',
            True: 'explicitly enabled',
            None: 'reset'
        }[self.whitelisted]

        return f'{self._get_header()} already {message}.'


_command_node = '{0.cog_name}.{0}'.format

def _extract_from_node(node):
    return node.partition('.')


def _get_class_name(obj):
    # Thanks discord.py
    return obj.__class__.__name__.replace('Text', '')


def _walk_parents(command):
    """Walks up a command's parent chain."""
    return iter(iterate(operator.attrgetter('parent'), command).__next__, None)

# Some converter utilities I guess


class CommandName(BotCommand):
    async def convert(self, ctx, arg):
        command = await super().convert(ctx, arg)

        root = command.root_parent or command
        if root.name in {'enable', 'disable', 'undo'} or root.cog_name == 'Owner':
            raise commands.BadArgument("You can't modify this command.")

        return _command_node(command)


class ModuleName(BotCogConverter):
    def _maybe_module(self, ctx, arg):
        parents = {c.__parent_category__.lower() for c in ctx.bot.cogs.values()}
        lowered = arg.lower()
        if lowered not in parents:
            raise commands.BadArgument('No module called {arg} found.')

        if lowered == 'owner':
            raise commands.BadArgument("You can't modify this cog...")

        return lowered

    async def convert(self, ctx, arg):
        try:
            cog = await super().convert(ctx, arg)
        except commands.BadArgument:
            return self._maybe_module(ctx, arg)
        else:
            name = type(cog).__name__

            if name in {'Permissions', 'Owner'}:
                raise commands.BadArgument("You can't modify this cog...")

            return f'{cog.__parent_category__}/{name}'

    @staticmethod
    def random_example(ctx):
        categories = {c.__parent_category__.lower() for c in ctx.bot.cogs.values()}
        return random.sample(categories, 1)[0]


PermissionEntity = disambiguate.union(discord.Member, discord.Role, discord.TextChannel)
Plonkable = disambiguate.union(discord.TextChannel, discord.Member)

# End of the converters I guess.


class Server(namedtuple('Server', 'server')):
    """This class is here to make sure that we can have an ID of None
    while still having the original server object.
    """
    __slots__ = ()

    @property
    def id(self):
        return None

    def __str__(self):
        return str(self.server)


class _DummyEntry(namedtuple('_DummyEntry', 'id')):
    """This class ensures we have a mentionable object for ->ignores"""
    __slots__ = ()

    @property
    def mention(self):
        return f'<Not Found: {self.id}>'


ENTITY_EXPLANATION = """
You can {action} {thing} for a channel, member, or role,
or any combination of the three.

(Keep in mind that names with more that one word must be
put in quotes.)

If you don't specify a channel, member, or role, it will
{action} {thing} for this server.
"""


# TODO: Make this an enum
_value_embed_mappings = {
    True: (0x00FF00, 'enabled', emoji_url('\N{WHITE HEAVY CHECK MARK}')),
    False: (0xFF0000, 'disabled', emoji_url('\N{NO ENTRY SIGN}')),
    None: (0x7289da, 'reset', emoji_url('\U0001f504')),
    -1: (0xFF0000, 'deleted', emoji_url('\N{PUT LITTER IN ITS PLACE SYMBOL}')),
}
_plonk_embed_mappings = {
    True: (0xf44336, 'plonk'),
    False: (0x4CAF50, 'unplonk'),
}
PLONK_ICON = emoji_url('\N{HAMMER}')


class Permissions(Cog):
    """Used for enabling or disabling commands for a channel, member,
    role, or even the whole server.
    """

    # These types of commands are usually extremely complex. The goal
    # of this was to be as simple as possible. Unfortunately while debugging
    # the thing I forgot how my own perms were resolved, so I guess I failed
    # in that regard.
    #
    # Most of these commands require Manage Server. while these can potentially
    # be dangerous, the worst that can happen is that you accidentally lock
    # yourself out. You can't lock these commands anyway, so nothing really
    # bad will happen, unlike having *overrides*, which are a million times
    # more dangerous.

    async def __global_check_once(self, ctx):
        if not ctx.guild:
            return True

        if await ctx.bot.is_owner(ctx.author):
            return True

        query = 'SELECT 1 FROM plonks WHERE guild_id = $1 AND entity_id IN ($2, $3) LIMIT 1;'
        row = await ctx.db.fetchrow(query, ctx.guild.id, ctx.author.id, ctx.channel.id)
        return row is None

    async def on_command_error(self, ctx, error):
        if isinstance(error, (PermissionDenied, InvalidPermission)):
            await ctx.send(error)

    async def __error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            if await ctx.bot.is_owner(ctx.author):
                return

            missing = [perm.replace('_', ' ').replace('guild', 'server').title()
                       for perm in error.missing_perms]

            message = (f"You need the {formats.human_join(missing)} permission, because "
                       "these types of commands are very advanced, I think.")
            # TODO: put this in an embed.
            await ctx.send(message)

    async def _set_one_permission(self, connection, guild_id, name, entity, whitelist):
        id = entity.id
        if whitelist is None:
            if id is None:
                query = 'DELETE FROM permissions WHERE guild_id = $1 AND name = $2 AND snowflake IS NULL;'
                status = await connection.execute(query, guild_id, name)
            else:
                query = 'DELETE FROM permissions WHERE guild_id = $1 AND name = $2 AND snowflake = $3;'
                status = await connection.execute(query, guild_id, name, id)
            count = status.partition(' ')[-2]

            if count == '0':
                raise InvalidPermission(
                    f'{name} was neither disabled nor enabled...', name, whitelist
                )
        else:
            query = """INSERT INTO permissions (guild_id, snowflake, name, whitelist)
                     VALUES ($1, $2, $3, $4)
                     ON CONFLICT (snowflake, name)
                     DO UPDATE SET whitelist = $4
                    """
            await connection.execute(query, guild_id, id, name, whitelist)

    async def _bulk_set_permissions(self, connection, guild_id, name, *entities, whitelist):
        ids = tuple(unique(e.id for e in entities))
        # This was actually extremely hard to do.
        #
        # What we actually need to do was to bulk-insert a bunch of records.
        # However, there is a chance that someone would've attempted to modify
        # a row that already exists -- they'd just want to change the whitelist
        # bool.
        #
        # Unfortunately, there is no easy way to do that, because bulk-update
        # doesn't return the rows that were modified. The only real way to do
        # this is to delete all the rows, then re-insert them through COPY.
        # This wreaks havoc on the indexes of the table, causing a major
        # performance penalty, but most of time you don't   be constantly
        # changing the permissions of a certain entity anyway.
        query = """DELETE FROM permissions
                   WHERE guild_id = $1 AND name = $2 AND snowflake = ANY($3::bigint[]);
                """
        status = await connection.execute(query, guild_id, name, ids)
        print(status)

        if whitelist is None:
            # We don't want it to recreate the permissions during a reset.
            return

        columns = ('guild_id', 'snowflake', 'name', 'whitelist')
        to_insert = [(guild_id, id, name, whitelist) for id in ids]

        await connection.copy_records_to_table('permissions', columns=columns, records=to_insert)

    async def _set_permissions(self, connection, guild_id, name, *entities, whitelist):
        # Because of the bulk-updating method above, we can't exactly run a
        # check to see if any of the rows already exist on the table, as that
        # would just be another wasted query.
        method = self._set_one_permission if len(entities) == 1 else self._bulk_set_permissions
        await method(connection, guild_id, name, *entities, whitelist=whitelist)

    @cache.cache(maxsize=None, make_key=lambda a, kw: a[-1])
    async def _get_permissions(self, connection, guild_id):
        query = 'SELECT name, snowflake, whitelist FROM permissions WHERE guild_id=$1'
        records = await connection.fetch(query, guild_id)

        lookup = defaultdict(lambda: (set(), set()))
        for name, snowflake, whitelist in records:
            lookup[snowflake][whitelist].add(name)

        # Converting this to a dict so future retrievals of this via cache
        # don't accidentally modify this.
        return dict(lookup)

    async def __global_check(self, ctx):
        if not ctx.guild:  # Custom permissions don't really apply in DMs
            return True

        if await ctx.bot.is_owner(ctx.author):
            return True

        # XXX: Should I have a check for if the table/relation actually exists?
        lookup = await self._get_permissions(ctx.db, ctx.guild.id)
        if not lookup:
            # "Fast" path
            return True

        # Do not disable the actual permission commands. Even though we prevent
        # it in the module and command subcommands, we disable everything in
        # `->disable all`, meaning these get disabled as well, causing strange
        # issues.
        root = ctx.command.root_parent or ctx.command
        if root in {self.enable, self.disable, self.undo}:
            return True

        dummy_server = Server(ctx.guild)

        objects = itertools.chain(
            [('user', ctx.author)],
            zip(itertools.repeat('role'), sorted(ctx.author.roles, reverse=True)),
            [('channel', ctx.channel),
             ('server', dummy_server)],
        )

        parent = ctx.cog.__parent_category__
        names = itertools.chain(
            map(_command_node, _walk_parents(ctx.command)),
            (f'{parent}/{ctx.command.cog_name}', parent, ALL_COMMANDS_KEY)
        )

        # The following code is roughly along the lines of this:
        # Apply guild-level denies first
        # then guild-level allows
        # then channel-level denies
        # then channel-level allows
        # ...
        # all the way down the user level.
        #
        # The levels go up the command tree, starting from the root command,
        # and ending at the actual sub command.
        #
        # However, there's one critical difference: we go in reverse order here,
        # starting from the user level, then ending at the guild level. This gives
        # the exact same result, because we're really looking for the last perm that
        # would be applied here. However, by going in reverse this allows for two
        # things:
        #
        # 1. Optimization: By returning early we don't have to evaluate all the
        #    permissions. This helps a lot as a lot of commands will be thrown at
        #    the bot.
        # 2. The ability to stop early and throw an exception indicating which
        #    command and which level it's disabled on. If we go forwards, we won't
        #    know the last perm that will be applied, but here we'll able to know
        #    because we're looking for the first perm.
        #
        for (typename, obj), name in itertools.product(objects, names):
            if obj.id not in lookup:  # more likely for an id to not be in here.
                continue

            if name in lookup[obj.id][True]:  # allow overrides deny
                return True

            elif name in lookup[obj.id][False]:
                raise PermissionDenied(f'{name} is denied on the {typename} level', name, obj)

        return True

    async def _display_embed(self, ctx, name=None, *entities, whitelist, type_):
        colour, action, icon = _value_embed_mappings[whitelist]

        def name_values():
            sorted_entities = sorted(entities, key=_get_class_name)
            for k, group in itertools.groupby(sorted_entities, _get_class_name):
                group = list(group)
                name = f'{k}{"s" * (len(group) != 1)}'
                value = truncate(', '.join(map(str, group)), 1024, '...')
                yield name, value

        if ctx.bot_has_embed_links():
            embed = (discord.Embed(colour=colour)
                     .set_author(name=f'{type_} {action}!', icon_url=icon)
                     )

            if name not in {ALL_COMMANDS_KEY, None}:
                cog, _, name = _extract_from_node(name)
                embed.add_field(name=type_, value=name or cog)

            for name, value in name_values():
                embed.add_field(name=name, value=value, inline=False)

            await ctx.send(embed=embed)
        else:
            cog, _, name = _extract_from_node(name)
            joined = '\n'.join(f'**{name}:** {value}' for name, value in name_values())
            message = f'Successfully {action} {type_.lower()} {name or cog}!\n\n{joined}'
            await ctx.send(message)

    async def _set_permissions_command(self, ctx, name, *entities, whitelist, type_):
        entities = entities or (Server(ctx.guild), )

        await self._set_permissions(ctx.db, ctx.guild.id, name, *entities, whitelist=whitelist)
        self._get_permissions.invalidate(None, None, ctx.guild.id)

        await self._display_embed(ctx, name, *entities, whitelist=whitelist, type_=type_)

    def _make_command(value, name, *, desc):
        format_entity = functools.partial(ENTITY_EXPLANATION.format, action=name.lower())

        participle = desc.split(' ', 1)[0][:-1]
        participle = (participle[:-1] if participle[-1] == 'e' else participle) + 'ing'

        base_doc_string = f'Group for {participle.lower()} commands or cogs.'
        cmd_doc_string = f'{desc} a command.\n{format_entity(thing="a command")}'
        cog_doc_string = f'{desc} a category.\n{format_entity(thing="a category")}'
        all_doc_string = (f'{desc} all commands.\n'
                          f'{format_entity(thing="all commands")}')

        # TODO: ->enable without ANY subcommands
        @commands.group(name=name, help=base_doc_string)
        @commands.has_permissions(manage_guild=True)
        async def group(self, ctx):
            # XXX: I'm not exactly sure whether this should be the same
            #      as ->enable command, or if should take cogs as well.
            #      The former might make it easier to parse and disambiguate,
            #      while the latter might be way simpler for the end user.
            #      (or harder since there are some commands that have the
            #       name as cogs.)
            #
            # Just gonna do some input checking for now.

            if ctx.invoked_subcommand:
                return

            arg = ctx.subcommand_passed
            if not arg:
                subs = '\n'.join(map(f'`{ctx.prefix}{{}}`'.format, ctx.command.commands))
                message = (f"{ctx.command.name.title()} what? You're gonna have "
                           f"to be a little more specific here... I think. Here "
                           f"are the commands:\n{subs}"
                           )
                return await ctx.send(message)

            # In case someone attempts to do for example, ->enable "random colour"
            arg = arg.strip('"')

            maybe_command = ctx.bot.get_command(arg)
            if maybe_command is not None:
                message = (f'Hm... this looks like a command... I think.\n'
                           f'Use `{ctx.command} command {arg} ` if '
                           f"you're planning to {ctx.command} it...?"
                           )
                return await ctx.send(message)

            lowered = arg.lower()
            if any(cog.lower() == lowered for cog in ctx.bot.cogs):
                message = (f'This looks like a cog... I think.\n'
                           f'Use `{ctx.command} cog {arg} ` if '
                           f"you're planning to {ctx.command} it...?"
                           )
                return await ctx.send(message) 

            subs = '\n'.join(map(f'`{ctx.prefix}{{0}}` - {{0.short_doc}}'.format,
                                 ctx.command.commands))
            message = ("\N{THINKING FACE} I don't even know what you want to "
                       f"{ctx.command}... here are the commands again... \n{subs}"
                       )
            await ctx.send(message)

        @group.command(
            name='command', help=cmd_doc_string,
            aliases=['cmd'], usage='<command> [channels, members or roles...]'
        )
        async def group_command(self, ctx, command: CommandName, *entities: PermissionEntity):
            await self._set_permissions_command(ctx, command, *entities,
                                                whitelist=value, type_='Command')

        # Providing these helper commands to allow users to "bulk"-disable certain
        # certain commands. Theoretically I COULD allow for ->enable command_or_module
        # but that would force me to make the commands case sensitive.
        #
        # Not sure whether that would be good or bad for the end user.
        @group.command(
            name='category', help=cog_doc_string,
            aliases=['cog', 'module'], usage='<category> [channels, members or roles...]'
        )
        async def group_category(self, ctx, category: ModuleName, *entities: PermissionEntity):
            await self._set_permissions_command(ctx, category, *entities,
                                                whitelist=value, type_='Category')

        @group.command(name='all', help=all_doc_string, usage='[channels, members or roles...]')
        async def group_all(self, ctx, *entities: PermissionEntity):
            await self._set_permissions_command(ctx, ALL_COMMANDS_KEY, *entities,
                                                whitelist=value, type_='All commands')

        # Must return all of these otherwise the subcommands won't get added
        # properly -- they will end up having no instance.
        return group, group_command, group_category, group_all

    # The actual commands... yes it's really short.
    enable, enable_command, enable_cog, enable_all = _make_command(True, 'enable', desc='Enables')
    disable, disable_command, disable_cog, disable_all = _make_command(False, 'disable',
                                                                       desc='Disables')
    _undo_desc = 'Resets (or undoes) the permissions for'
    undo, undo_command, undo_cog, undo_all = _make_command(None, 'undo', desc=_undo_desc)
    del _make_command, _undo_desc

    @commands.command(name='resetperms', aliases=['clearperms'])
    @commands.has_permissions(administrator=True)
    async def reset_perms(self, ctx):
        """Clears *all* the permissions for commands and cogs.

        This is a very risky action. Once you delete it, it's gone.
        You'll have to replace them all. Only do this if you *really*
        messed up.

        If you wish to just delete just one perm, or multiple, use
        `{prefix}undo` instead.

        """
        query = 'DELETE FROM permissions WHERE guild_id = $1;'
        status = await ctx.db.execute(query, ctx.guild.id)
        print(status)
        self._get_permissions.invalidate(None, None, ctx.guild.id)

        await self._display_embed(ctx, None, Server(ctx.guild),
                                  whitelist=-1, type_='All permissions')

    async def _bulk_ignore_entries(self, ctx, entries):
        guild_id = ctx.guild.id
        query = 'SELECT entity_id FROM plonks WHERE guild_id = $1;'

        ignored = {r[0] for r in await ctx.db.fetch(query, guild_id)}
        to_insert = [(guild_id, e.id) for e in entries if e.id not in ignored]

        await ctx.db.copy_records_to_table(
            'plonks',
            columns=('guild_id', 'entity_id'),
            records=to_insert
        )

    async def _display_plonked(self, ctx, entries, plonk):
        # things = channels, members

        colour, action = _plonk_embed_mappings[plonk]

        def name_values():
            for thing in map(list, partition(lambda e: isinstance(e, discord.TextChannel), entries)):
                if not thing:
                    continue

                name = f'{_get_class_name(thing[0])}{"s" * (len(thing) != 1)}'
                value = truncate(', '.join(map(str, thing)), 1024, '...')
                yield name, value

        if ctx.bot_has_embed_links():
            embed = (discord.Embed(colour=colour)
                     .set_author(name=f'{action.title()} successful!', icon_url=PLONK_ICON)
                     )
            for name, value in name_values():
                embed.add_field(name=name, value=value, inline=False)
            await ctx.send(embed=embed)
        else:
            joined = '\n'.join(f'**{name}:** {value}' for name, value in name_values())
            message = f'Successfully {ctx.command}d\n{joined}'
            await ctx.send(message)

    @commands.command(aliases=['plonk'])
    @commands.has_permissions(manage_guild=True)
    async def ignore(self, ctx, *channels_or_members: Plonkable):
        """Ignores text channels or members from using the bot.

        If no channel or member is specified, the current channel is ignored.
        """
        channels_or_members = channels_or_members or [ctx.channel]

        if len(channels_or_members) == 1:
            thing = channels_or_members[0]
            query = 'INSERT INTO plonks (guild_id, entity_id) VALUES ($1, $2);'

            try:
                await ctx.db.execute(query, ctx.guild.id, thing.id)
            except asyncpg.UniqueViolationError:
                return await ctx.send(f"I'm already ignoring {thing}...")
        else:
            await self._bulk_ignore_entries(ctx, channels_or_members)

        await self._display_plonked(ctx, channels_or_members, plonk=True)

    @commands.command(aliases=['unplonk'])
    @commands.has_permissions(manage_guild=True)
    async def unignore(self, ctx, *channels_or_members: Plonkable):
        """Allows channels or members to use the bot again.

        If no channel or member is specified, it unignores the current channel.
        """
        entities = channels_or_members or (ctx.channel, )
        if len(entities) == 1:
            query = 'DELETE FROM plonks WHERE guild_id = $1 AND entity_id = $2;'
            await ctx.db.execute(query, ctx.guild.id, entities[0].id)
        else:
            query = 'DELETE FROM plonks WHERE guild_id = $1 AND entity_id = ANY($2::bigint[]);'
            await ctx.db.execute(query, ctx.guild.id, [e.id for e in entities])

        await self._display_plonked(ctx, entities, plonk=False)

    @commands.command(aliases=['plonks'])
    @commands.has_permissions(manage_guild=True)
    async def ignores(self, ctx):
        """Tells you what channels or members are currently ignored in this server."""
        query = 'SELECT entity_id FROM plonks WHERE guild_id = $1;'

        get_ch, get_m = ctx.guild.get_channel, ctx.guild.get_member
        entries = [
            (get_ch(e_id) or get_m(e_id) or _DummyEntry(e_id)).mention
            for e_id, in await ctx.db.fetch(query, ctx.guild.id)
        ]

        if not entries:
            return await ctx.send("I'm not ignoring anything here...")

        pages = ListPaginator(ctx, entries, title=f"Currently ignoring...", lines_per_page=20)
        await pages.interact()


def setup(bot):
    bot.add_cog(Permissions(bot))
