import asyncio
import asyncpg
import copy
import discord

from discord.ext import commands
from functools import partial

from .utils import disambiguate
from .utils.context_managers import temp_attr
from .utils.misc import str_join

from core.cog import Cog


class LowerRole(commands.RoleConverter):
    async def convert(self, ctx, arg):
        role = await super().convert(ctx, arg)
        author = ctx.author

        top_role = author.top_role
        if role >= top_role and author != ctx.guild.owner:
            raise commands.BadArgument(f"This role ({role}) is higher than or equal "
                                       f"to your highest role ({top_role}).")

        return role


class LowerRoleSearch(disambiguate.DisambiguateRole, LowerRole):
    pass


async def _check_role(ctx, role, thing):
    if role.managed:
        raise commands.BadArgument("This is an integration role, I can't assign this to anyone!")

    # Assigning people with the @everyone role is not possible
    if role.is_default():
        message = ("Wow, good job. I'm just gonna grab some popcorn now..."
                   if ctx.message.mention_everyone else
                   "You're lucky that didn't do anything...")
        raise commands.BadArgument(message)

    if role.permissions.administrator:
        message = ("This role has the Administrator permission. "
                   "It's very dangerous and can lead to terrible things. "
                   f"Are you sure you wanna make this {thing} role?")
        try:
            result = await ctx.ask_confirmation(message)
        except asyncio.TimeoutError:
            raise commands.BadArgument("Took too long. Aborting...")
        else:
            if not result:
                raise commands.BadArgument("Aborted.")


async def _get_self_roles(ctx):
    server = ctx.guild
    query = 'SELECT role_id FROM selfroles WHERE guild_id = $1;'

    getter = partial(discord.utils.get, server.roles)
    roles = (getter(id=row[0]) for row in await ctx.db.fetch(query, ctx.guild.id))
    # in case there are any non-existent roles
    return [r for r in roles if r]


class SelfRole(disambiguate.DisambiguateRole):
    async def convert(self, ctx, arg):
        if not ctx.guild:
            raise commands.NoPrivateMessage

        self_roles = await _get_self_roles(ctx)
        if not self_roles:
            message = ("This server has no self-assignable roles. "
                       f"Use `{ctx.prefix}asar` to add one.")
            raise commands.BadArgument(message)

        temp_guild = copy.copy(ctx.guild)
        temp_guild.roles = self_roles

        with temp_attr(ctx, 'guild', temp_guild):
            try:
                return await super().convert(ctx, arg)
            except commands.BadArgument:
                raise commands.BadArgument(f'{arg} is not a self-assignable role...')


class AutoRole(disambiguate.DisambiguateRole):
    async def convert(self, ctx, arg):
        if not ctx.guild:
            raise commands.NoPrivateMessage

        role = await super().convert(ctx, arg)
        await _check_role(ctx, role, thing='an auto-assign')
        return role


class Roles(Cog):
    """Commands that are related to roles.

    Self-assignable, auto-assignable, and general role-related
    commands are in this cog.
    """

    def __init__(self, bot):
        self.bot = bot

    def __local_check(self, ctx):
        return bool(ctx.guild)

    @commands.command(name='addselfrole', aliases=['asar', ])
    @commands.has_permissions(manage_roles=True, manage_guild=True)
    async def add_self_role(self, ctx, *, role: LowerRoleSearch):
        """Adds a self-assignable role to the server

        A self-assignable role is one that you can assign to yourself
        using `{prefix}iam` or `{prefix}selfrole`
        """
        await _check_role(ctx, role, thing='a self-assignable')
        try:
            query = 'INSERT INTO selfroles (guild_id, role_id) VALUES ($1, $2);'
            await query.execute(query, ctx.guild.id, role.id)
        except asyncpg.UniqueViolationError:
            await ctx.send(f'{role} is already a self-assignable role.')
        else:
            await ctx.send(f"**{role}** is now a self-assignable role!")

    @commands.command(name='removeselfrole', aliases=['rsar', ])
    @commands.has_permissions(manage_roles=True, manage_guild=True)
    async def remove_self_role(self, ctx, *, role: SelfRole):
        """Removes a self-assignable role from the server

        A self-assignable role is one that you can assign to yourself
        using `{prefix}iam` or `{prefix}selfrole`
        """
        query = 'DELETE FROM selfroles WHERE role_id = $1;'
        await ctx.db.execute(query, ctx.guild.id)
        await ctx.send(f"**{role}** is no longer a self-assignable role!")

    @commands.command(name='listselfrole', aliases=['lsar'])
    async def list_self_role(self, ctx):
        """List all the self-assignable roles in the server

        A self-assignable role is one that you can assign to yourself
        using `{prefix}iam` or `{prefix}selfrole`
        """
        self_roles = await _get_self_roles(ctx)
        msg = (f'List of self-assignable roles: \n{str_join(", ", self_roles)}'
               if self_roles else 'There are no self-assignable roles...')
        await ctx.send(msg)

    @commands.command()
    async def iam(self, ctx, *, role: SelfRole):
        """Gives a self-assignable role (and only a self-assignable role) to yourself."""
        if role in ctx.author.roles:
            return await ctx.send(f"You are {role} already...")

        await ctx.author.add_roles(role)
        await ctx.send(f"You are now **{role}**... I think.")

    @commands.command()
    async def iamnot(self, ctx, *, role: SelfRole):
        """Removes a self-assignable role (and only a self-assignable role) from yourself."""
        if role not in ctx.author.roles:
            return await ctx.send(f"You aren't {role} already...")

        await ctx.author.remove_roles(role)
        await ctx.send(f"You are no longer **{role}**... probably.")

    @commands.command()
    async def selfrole(self, ctx, *, role: SelfRole):
        """Gives or removes a self-assignable role (and only a self-assignable role)

        This depends on whether or not you have the role already.
        If you don't, it gives you the role. Otherwise it removes it.
        """
        author = ctx.author
        msg, role_action = ((f"You are no longer **{role}**... probably.", author.remove_roles)
                            if role in author.roles else
                            (f"You are now **{role}**... I think.", author.add_roles))
        await role_action(role)
        await ctx.send(msg)

    # ----------- Auto-Assign Role commands -----------------
    @commands.command(name='autorole', aliases=['aar'])
    @commands.has_permissions(manage_roles=True, manage_guild=True)
    async def auto_assign_role(self, ctx, role: AutoRole):
        """Sets a role that new members will get when they join the server.

        This can be removed with `{prefix}delautorole` or `{prefix}daar`
        """
        # While technically expensive to do two queries, we need this for the
        # sake of UX, as I'm not sure if there's an easier way of checking if
        # this was the self-assignable role.
        query = 'SELECT 1 FROM autoroles WHERE role_id = $1;'
        if await ctx.db.fetchrow(query, role.id):
            return await ctx.send("You silly baka, you've already made this auto-assignable!")

        query = """INSERT INTO autoroles (guild_id, role_id) VALUES ($1, $2)
                   ON CONFLICT (guild_id)
                   DO UPDATE SET role_id = $2;
                """
        await ctx.db.execute(query, ctx.guild.id, role.id)

        await ctx.send(f"I'll now give new members {role}. Hope that's ok with you (and them :p)")

    @commands.command(name='delautorole', aliases=['daar'])
    @commands.has_permissions(manage_roles=True, manage_guild=True)
    async def del_auto_assign_role(self, ctx):
        """Removes the auto-assign-role set by `{prefix}autorole`"""
        query = 'DELETE FROM autoroles WHERE guild_id = $1;'

        status = await ctx.db.execute(query, ctx.guild.id)
        if status[-1] == '0':
            return await ctx.send("There's no auto-assign role here...")

        await ctx.send("Ok, no more auto-assign roles :(")

    async def _add_auto_role(self, member):
        server = member.guild
        query = 'SELECT role_id FROM autoroles WHERE guild_id = $1;'

        row = await self.bot.pool.fetchrow(query, server.id)
        if row is None:
            return

        # TODO: respect the high verification level, and check perms.
        await member.add_roles(discord.Object(id=row[0]))

    @commands.command(name='addrole', aliases=['ar'])
    @commands.has_permissions(manage_roles=True)
    async def add_role(self, ctx, member: discord.Member, *, role: LowerRole):
        """Adds a role to a user

        This role must be lower than both the bot's highest role and your highest role.
        """
        if role in member.roles:
            return await ctx.send(f'{member} already has **{role}**... \N{NEUTRAL FACE}')

        await member.add_roles(role)
        await ctx.send(f"Successfully gave {member} **{role}**, I think.")

    @commands.command(name='removerole', aliases=['rr'])
    @commands.has_permissions(manage_roles=True)
    async def remove_role(self, ctx, member: discord.Member, *, role: LowerRole):
        """Removes a role from a user

        This role must be lower than both the bot's highest role and your highest role.
        Do not confuse this with `{prefix}deleterole`, which deletes a role from the server.
        """
        if role not in member.roles:
            return await ctx.send(f"{member} doesn't have **{role}**... \N{NEUTRAL FACE}")

        await member.remove_roles(role)
        await ctx.send(f"Successfully removed **{role}** from {member}, I think.")

    @commands.command(name='createrole', aliases=['crr'])
    @commands.has_permissions(manage_roles=True)
    async def create_role(self, ctx, *, name: str):
        """Creates a role with a given name."""
        reason = f'Created through command from {ctx.author} ({ctx.author.id})'
        await ctx.guild.create_role(reason=reason, name=name)
        await ctx.send(f"Successfully created **{name}**!")

    @commands.command(name='deleterole', aliases=['delr'])
    @commands.has_permissions(manage_roles=True)
    async def delete_role(self, ctx, *, role: LowerRole):
        """Deletes a role from the server

        Do not confuse this with `{prefix}removerole`, which removes a role from a member.
        """
        await role.delete()
        await ctx.send(f"Successfully deleted **{role.name}**!")

    @add_role.error
    @remove_role.error
    @create_role.error
    @delete_role.error
    async def role_error(self, ctx, error):
        if not isinstance(error, commands.CommandInvokeError):
            return

        verb = ctx.command.callback.__name__.partition('_')[0]
        role = ctx.kwargs['name'] if verb == 'create' else ctx.kwargs['role']

        print(type(error.original))
        if isinstance(error.original, discord.Forbidden):
            if not ctx.guild.me.permissions_in(ctx.channel).manage_roles:
                await ctx.send('{ctx.author.mention}, I need the Manage roles permission pls...')

            # We can't modify an add, remove, or delete an integration role, obviously.
            elif getattr(role, 'managed', False):       # ->createrole uses a string for the role.
                await ctx.send(f"{role} is an intergration role, I can't do anything with that!")

            # Assume the role was too high otherwise.
            else:
                await ctx.send('The role was higher than my highest role. '
                               'Check the hierachy please! \U0001f605')

        elif isinstance(error.original, discord.HTTPException):      # Something strange happened.
            # will probably refactor this out into another function later.
            if verb.endswith('e'):
                verb = verb[:-1]

            message = (
                f'{verb.title()}ing {role} failed for some reason... '
                'Send this error to the dev if you can:\n'
                f'{type(error).__name__}: {error}'
            )

            await ctx.send(message)

    async def on_member_join(self, member):
        await self._add_auto_role(member)


def setup(bot):
    bot.add_cog(Roles(bot))
