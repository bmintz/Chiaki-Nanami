import asyncio
import copy
import discord
import functools
import inspect
import random
import re
import sys as _sys

from discord.ext import commands

from .manager import SessionManager

from ..utils.context_managers import temp_attr
from ..utils.converter import CheckedMember, NoSelfArgument
from ..utils.formats import multi_replace

from core.cog import Cog

_clean_sig = functools.partial(multi_replace, replacements={**dict.fromkeys('<>[]', ''), '|': '/'})


# templates for the plugins...

two_player_plugin_template = '''\
class {typename}(Cog, name='{game_name}'):
    def __init__(self):
        self.manager = SessionManager()

    async def _help(self, ctx):
        description = 'To start a game, use one of the commands below!'
        create, invite, join = sorted(ctx.command.commands, key=str)
        create_name = 'To start a game for anyone to join, type'
        invite_name = 'To invite a specific person, type'
        accept_name = 'To join a game, type'
        make_sig = lambda cmd: f'`{{ctx.prefix}}{{_clean_sig(cmd.signature)}}`'

        embed = (discord.Embed(title='{game_name} Help', colour=0x00FF00, description=description)
                .add_field(name=create_name, value=make_sig(create), inline=False)
                .add_field(name=invite_name, value=make_sig(invite), inline=False)
                .add_field(name=accept_name, value=make_sig(join)  , inline=False)
                )
        await ctx.send(embed=embed)

    @staticmethod
    def _make_invite_embed(ctx, member):
        action = 'invited you to' if member else 'created'
        title = f'{{ctx.author}} has {{action}} a game of {game_name}!'
        description = (f'Type `{{ctx.prefix}}{{ctx.command.root_parent}} join` to join and play!\\n'
                        'This will expire in 10 minutes.')

        return (discord.Embed(colour=0x00FF00, description=description)
               .set_author(name=title)
               .set_thumbnail(url=ctx.author.avatar_url)
               )

    async def _start_invite(self, ctx, member):
        invite_embed = self._make_invite_embed(ctx, member)

        if member is None:
            await ctx.send(embed=invite_embed)
            return

        # attempt to DM them, if that fails, fall back to mentioning them on the channel.
        try:
            temp_desc = (f'Type `{{ctx.prefix}}{{ctx.command.root_parent}} join` in channel '
                         f'**#{{ctx.channel}}** in **{{ctx.guild}}** to join and play!\\n'
                          'This will expire in 10 minutes.')

            with temp_attr(invite_embed, 'description', temp_desc):
                await member.send(embed=invite_embed)
        except discord.HTTPException:
            await ctx.send(member.mention, embed=invite_embed)
        else:
            await ctx.send(f'DM has been sent to {{member}}!')

    async def _do_game(self, ctx, member):
        if self.manager.session_exists(ctx.channel):
            return await ctx.send("There's a Connect-4 running in this channel "
                                  "right now. I think... ;-;")

        with self.manager.temp_session(ctx.channel, {cls}(ctx, member)) as inst:
            await self._start_invite(ctx, member)
            await inst.wait_for_opponent()
            stats = await inst.run()
            if stats.winner is None:
                return await ctx.send('It looks like nobody won :(')

            user = stats.winner.user
            winner_embed = (discord.Embed(colour=0x00FF00, description=f'Game took {{stats.turns}} turns to complete.')
                           .set_thumbnail(url=user.avatar_url)
                           .set_author(name=f'{{user}} is the winner!')
                           )

            await ctx.send(embed=winner_embed)

    @commands.group(name={name!r}, aliases={aliases!r})
    async def group(self, ctx):
        """Shows how to start up {game_name}"""
        if not (ctx.invoked_subcommand or ctx.subcommand_passed):
            await self._help(ctx)

    @group.command(name='create', aliases=['start'])
    async def group_create(self, ctx):
        """Starts a game of {game_name} where anyone can join"""
        await self._do_game(ctx, None)

    @group.command(name='invite')
    async def group_invite(self, ctx, *, challenger: CheckedMember(offline=False, bot=False, include_self=False)):
        """Invites a specific person to a game of {game_name}"""
        await self._do_game(ctx, challenger)

    @group.command(name='join', aliases=['accept'])
    async def group_join(self, ctx):
        """Joins a game of {game_name}."""
        session = self.manager.get_session(ctx.channel)
        if session is None:
            return await ctx.send(f'There is no {game_name} game for you to join...')

        if session.is_running():
            return await ctx.send(f"Sorry, there's already a {game_name} game running in this channel... ;-;")

        session.opponent = ctx.author
        await ctx.send(f"Alright {{ctx.author.mention}}. You're in!")

    @group_create.error
    @group_invite.error
    @group_join.error
    async def error(self, ctx, error):
        cause = error.__cause__
        if isinstance(cause, ValueError):
            await ctx.send(str(cause))
        if isinstance(cause, asyncio.TimeoutError):
            await ctx.send('No one joined in time... :(')
        if isinstance(error, NoSelfArgument):
            message = random.choice((
                "Don't play with yourself. x3",
                "You should mention someone else over there. o.o",
                "Self inviting, huh... :eyes:",
            ))
            await ctx.send(message)
'''

def make_plugin(typename, template, cls=None, name=None, *, verbose=False, module=None,
                game_name=None, aliases=()):
    """Returns a plugin for a given template and class name.

    This is used because as of right now, there is no easy way to subclass a
    certain plugin and get unique commands, as all inherited commands
    are the same object.
    """

    # This is a hack.
    name = name or typename.lower()
    game_name = game_name or re.sub(r"(\w)([A-Z])", r"\1 \2", typename)

    class_definition = template.format(
        typename=typename,
        name=name,
        aliases=aliases,
        cls=cls.__name__,
        game_name=game_name
    )

    namespace = {'__name__': 'namedtuple_%s' % typename, **globals(), cls.__name__: cls}
    exec(class_definition, namespace)
    result = namespace[typename]
    result._source = class_definition
    if verbose:
        print(result._source)

    # In a discord.py context, the __module__ variable needs to be set this way
    # because Bot.remove_cog relies on it when unloading the extension.
    # Failure to set it properly means the extension and the cog's module won't
    # match, which means that the cog will not unload, making a subsequent reload
    # fail as commands in that cog would be registered already.
    if module is None:
        try:
            module = _sys._getframe(1).f_globals.get('__name__', '__main__')
        except (AttributeError, ValueError):
            pass
    if module is not None:
        result.__module__ = module

    return result

two_player_plugin = functools.partial(make_plugin, template=two_player_plugin_template)
