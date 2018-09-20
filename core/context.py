import collections
import contextlib
import functools
import sys

import discord
from discord.ext import commands


class _ContextSession(collections.namedtuple('_ContextSession', 'ctx')):
    __slots__ = ()

    def __await__(self):
        return self.ctx._acquire().__await__()

    async def __aenter__(self):
        return await self.ctx._acquire()

    async def __aexit__(self, exc_type, exc, tb):
        return await self.ctx._release(exc_type, exc, tb)


class Context(commands.Context):
    # Default for whether or not the global error handlers should ignore errors
    # in commands with local error handlers.
    __bypass_local_error__ = False

    # Used for getting the current parameter when generating an example
    _current_parameter = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.db = None

    @property
    def pool(self):
        return self.bot.pool

    @property
    def clean_prefix(self):
        """The cleaned up invoke prefix. (mentions are @name instead of <@id>)."""
        user = self.bot.user
        return self.prefix.replace(user.mention, f'@{user.name}')

    async def _acquire(self):
        if self.db is None:
            self.db = await self.pool.acquire()
        return self.db

    def acquire(self):
        """Acquires a database session.

        Can be used in an async context manager: ::
            async with ctx.acquire():
                await ctx.db.execute(...)
        or: ::
            await ctx.acquire()
            try:
                await ctx.db.execute(...)
            finally:
                await ctx.release()
        """
        # DatabaseInterface.get_session doesn't support a timeout kwarg sadly...
        return _ContextSession(self)

    async def _release(self, exc_type, exc, tb):
        """Internal method used for properly propagating the exceptions
        in the session's __aexit__.

        This is the method that is called automatically by the bot,
        NOT Context.release.
        """
        if self.db is not None:
            await self.pool.release(self.db)
            self.db = None

    async def release(self):
        """Closes the current database session.

        Useful if needed for "long" interactive commands where
        we want to release the connection and re-acquire later.
        """
        return await self._release(*sys.exc_info())

    # Credit to Danny#0007 for making the original
    async def confirm(self, message, *, timeout=60.0, delete_after=True, reacquire=True,
                      author_id=None, destination=None):
        """Prompts the user with either yes or no."""

        # We can also wait for a message confirmation as well. This is faster, but
        # it's risky if there are two prompts going at the same time.
        # TODO: Possibly support messages again?

        destination = destination or self.channel
        with contextlib.suppress(AttributeError):
            if not destination.permissions_for(self.me).add_reactions:
                raise RuntimeError('Bot does not have Add Reactions permission.')

        config = self.bot.emoji_config
        confirm_emoji, deny_emoji = emojis = [config.confirm, config.deny]
        is_valid_emoji = frozenset(map(str, emojis)).__contains__

        instructions = f'{confirm_emoji} \N{EM DASH} Yes\n{deny_emoji} \N{EM DASH} No'

        if isinstance(message, discord.Embed):
            message.add_field(name="Choices", value=instructions, inline=False)
            msg = await destination.send(embed=message)
        else:
            message = f'{message}\n\n{instructions}'
            msg = await destination.send(message)

        author_id = author_id or self.author.id

        def check(data):
            return (data.message_id == msg.id
                    and data.user_id == author_id
                    and is_valid_emoji(str(data.emoji)))

        for em in emojis:
            await msg.add_reaction(em)

        if reacquire:
            await self.release()

        try:
            data = await self.bot.wait_for('raw_reaction_add', check=check, timeout=timeout)
            return str(data.emoji) == str(confirm_emoji)
        finally:
            if reacquire:
                await self.acquire()

            if delete_after:
                await msg.delete()

    ask_confirmation = confirm

    def bot_has_permissions(self, **permissions):
        perms = self.channel.permissions_for(self.me)
        return all(getattr(perms, perm) == value for perm, value in permissions.items())

    bot_has_embed_links = functools.partialmethod(bot_has_permissions, embed_links=True)
