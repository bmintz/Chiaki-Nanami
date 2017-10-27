import asyncio
import contextlib
import discord
import functools
import random

from discord.ext import commands

from core.cog import Cog

from ..utils.converter import CheckedMember, NoSelfArgument


class _TwoPlayerWaiter:
    def __init__(self, author, recipient):
        self._author = author
        self._recipient = recipient
        self._future = None
        self._closer = None
        self._event = asyncio.Event()

    def wait(self):
        future = self._future
        if future is None:
            future = self._future = asyncio.ensure_future(asyncio.wait_for(self._event.wait(), timeout=300))
        return future

    def confirm(self, member):
        if self._author == member:
            raise RuntimeError("You can't join a game that you've created. Are you really that lonely?")

        if self._recipient is None:
            self._recipient = member

        elif member != self._recipient:
            raise RuntimeError('This game is not for you!')

        self._event.set()

    def cancel(self, member):
        if self._recipient != member:
            return False

        self._closer = member
        return self._future.cancel()

    def cancel(self, member):
        if self._author != member:
            return False

        self._closer = member
        return self._future.cancel()

    def done(self):
        return bool(self._future and self._future.done())


@contextlib.contextmanager
def _swap_item(obj, item, new_val):
    obj[item] = new_val
    try:
        yield 
    finally:
        if item in obj:
            del obj[item]


@contextlib.contextmanager
def _dummy_cm(*args, **kwargs):
    yield


_two_player_help = '''
Starts a game of {name}

You can specify a user to invite them to play with
you. Leaving out the user creates a game that anyone
can join.
'''

_two_player_join_help = '''
Joins a {name} game.

This either must be for you, or for everyone.
'''

_two_player_decline_help = '''
Declines a {name} game.

This game must be for you. (i.e. through `{cmd} @user`)
'''

_two_player_close_help = '''
Closes a {name} game, stopping anyone from joining.

You must be the creator of the game.
'''

_two_player_create_help = 'Deprecated alias to `{name}`.'
_two_player_invite_help = 'Deprecated alias to `{name} @user`.'


_MemberConverter = CheckedMember(offline=False, bot=False, include_self=False)


class TwoPlayerGameCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self.running_games = {}
        self._invited_games = {}

    def __init_subclass__(cls, *, game_cls, cmd=None, aliases=(), **kwargs):
        super().__init_subclass__(**kwargs)

        cls.__game_class__ = game_cls
        cmd_name = cmd or cls.__name__.lower()

        group_help = _two_player_help.format(name=cls.name)
        group_command = commands.group(
            name=cmd_name, aliases=aliases, help=group_help, invoke_without_command=True
        )(cls._game)

        gc = group_command.command
        create_help = _two_player_create_help.format(name=cmd_name)
        create_command = gc(name='create', help=create_help)(cls._game_create)

        invite_help = _two_player_invite_help.format(name=cmd_name)
        invite_command = gc(name='invite', help=invite_help)(cls._game_invite)

        join_help = _two_player_join_help.format(name=cls.name)
        join_command = gc(name='join', help=join_help)(cls._game_join)

        decline_help = _two_player_decline_help.format(name=cls.name, cmd=cmd_name)
        decline_command = gc(name='decline', help=decline_help)(cls._game_decline)

        close_help = _two_player_close_help.format(name=cls.name, cmd=cmd_name)
        close_command = gc(name='close', help=close_help)(cls._game_close)

        setattr(cls, f'{cmd_name}', group_command)
        setattr(cls, f'{cmd_name}_create', create_command)
        setattr(cls, f'{cmd_name}_invite', invite_command)
        setattr(cls, f'{cmd_name}_join', join_command)
        setattr(cls, f'{cmd_name}_decline', decline_command)
        setattr(cls, f'{cmd_name}_close', close_command)
        setattr(cls, f'_{cls.__name__}__error', cls._error)

    async def _error(self, ctx, error):
        if isinstance(error, NoSelfArgument):
            message = random.choice((
                "Don't play with yourself. x3",
                "You should mention someone else over there. o.o",
                "Self inviting, huh... :eyes:",
            ))
            await ctx.send(message)
        elif issubclass(type(error), commands.BadArgument) and not type(error) is commands.BadArgument:
            await ctx.send(error)

    def _create_invite(self, ctx, member):
        action = 'invited you to' if member else 'created'
        title = f'{ctx.author} has {action} a game of {self.__class__.name}!'
        description = (
            f'Type `{ctx.prefix}{ctx.command.root_parent or ctx.command} join` to join and play!\n'
            'This will expire in 5 minutes.'
        )

        return (discord.Embed(colour=0x00FF00, description=description)
               .set_author(name=title)
               .set_thumbnail(url=ctx.author.avatar_url)
               )

    async def _invite_member(self, ctx, member):
        invite_embed = self._create_invite(ctx, member)

        if member is None:
            await ctx.send(embed=invite_embed)
        else:
            await ctx.send(f'{member.mention}, you have a challenger!', embed=invite_embed)

    async def _end_game(self, ctx, inst, result):
        if result.winner is None:
            return await ctx.send('It looks like nobody won :(')

        user = result.winner.user
        winner_embed = (discord.Embed(colour=0x00FF00, description=f'Game took {result.turns} turns to complete.')
                        .set_thumbnail(url=user.avatar_url)
                        .set_author(name=f'{user} is the winner!')
                       )

        await ctx.send(embed=winner_embed)

    async def _game(self, ctx, *, member: _MemberConverter = None):
        if ctx.channel.id in self.running_games:
            return await ctx.send(f"There's a {self.__class__.name} game already running in this channel...")

        if member is not None:
            pair = (ctx.author.id, member.id)
            channel_id = self._invited_games.get(pair)
            if channel_id:
                return await ctx.send(
                    "Um, you've already invited them in "
                    f"<#{channel_id}>, please don't spam them.."
                )

            cm = _swap_item(self._invited_games, pair, ctx.channel.id)
        else:
            cm = _dummy_cm()

        put_in_running = functools.partial(_swap_item, self.running_games, ctx.channel.id)

        with cm:
            await self._invite_member(ctx, member)
            with put_in_running( _TwoPlayerWaiter(ctx.author, member)):
                waiter = self.running_games[ctx.channel.id]
                try:
                    await waiter.wait()
                except asyncio.TimeoutError:
                    if member:
                        return await ctx.send(f"{member.mention} couldn't join in time... :/")
                    else:
                        return await ctx.send('No one joined in time. :(')
                except asyncio.CancelledError:
                    if waiter._closer == ctx.author:
                        msg = f'{ctx.author.mention} has closed the game. False alarm everyone...'
                    else:
                        msg = f'{ctx.author.mention}, {member} declined your challenge. :('
                    return await ctx.send(msg)

            with put_in_running(self.__game_class__(ctx, waiter._recipient)):
                inst = self.running_games[ctx.channel.id]
                result = await inst.run()

            await self._end_game(ctx, inst, result)

    async def _game_join(self, ctx):
        waiter = self.running_games.get(ctx.channel.id)
        if waiter is None:
            return await ctx.send(f"There's no {self.__class__.name} for you to join...")

        if not isinstance(waiter, _TwoPlayerWaiter):
            return await ctx.send('Sorry... you were late. ;-;')

        try:
            waiter.confirm(ctx.author)
        except RuntimeError as e:
            await ctx.send(e)
        else:
            await ctx.send(f'Alright {ctx.author.mention}, good luck!')

    async def _game_decline(self, ctx):
        waiter = self.running_games.get(ctx.channel.id)
        if waiter is None:
            return await ctx.send(f"There's no {self.__class__.name} for you to decline...")

        if isinstance(waiter, _TwoPlayerWaiter) and waiter.cancel(ctx.author):
            with contextlib.suppress(discord.HTTPException):
                await ctx.message.add_reaction('\U00002705')

    async def _game_close(self, ctx):
        waiter = self.running_games.get(ctx.channel.id)
        if waiter is None:
            return await ctx.send(f"There's no {self.__class__.name} for you to decline...")

        if isinstance(waiter, _TwoPlayerWaiter) and waiter.cancel(ctx.author):
            with contextlib.suppress(discord.HTTPException):
                await ctx.message.add_reaction('\U00002705')

    async def _game_create(self, ctx):
        await ctx.send(f'This subcommand is deprecated, use `->ttt` instead.')
        await ctx.invoke(ctx.command.root_parent)

    async def _game_invite(self, ctx, *, member: _MemberConverter):
        await ctx.send(f'This subcommand is deprecated, use `->ttt @{member}` instead.')
        await ctx.invoke(ctx.command.root_parent, member=member)
