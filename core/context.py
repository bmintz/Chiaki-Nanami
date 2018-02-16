import asyncio
import collections
import contextlib
import discord
import functools
import random
import sys

from discord.ext import commands
from itertools import starmap

from cogs.utils.formats import human_join


CHIAKI_EMOJI_REPO_GUILD_ID = 409305485720944651

_DEFAULT_MISSING_PERMS_ACTIONS = {
    'embed_links': 'embeds',
    'attach_files': 'upload stuffs',
}


class _ContextSession(collections.namedtuple('_ContextSession', 'ctx')):
    __slots__ = ()

    def __await__(self):
        return self.ctx._acquire().__await__()

    async def __aenter__(self):
        return await self.ctx._acquire()

    async def __aexit__(self, exc_type, exc, tb):
        return await self.ctx._release(exc_type, exc, tb)


class Context(commands.Context):
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

    async def disambiguate(self, matches, transform=str, *, tries=3):
        if not matches:
            raise ValueError('No results found.')

        num_matches = len(matches)
        if num_matches == 1:
            return matches[0]

        entries = '\n'.join(starmap('{0}: {1}'.format, enumerate(map(transform, matches), 1)))

        permissions = self.channel.permissions_for(self.me)
        if permissions.embed_links:
            # Build the embed as we go. And make it nice and pretty.
            embed = discord.Embed(colour=self.bot.colour, description=entries)
            embed.set_author(name=f"There were {num_matches} matches found... Which one did you mean?")

            index = random.randrange(len(matches))
            instructions = f'Just type the number.\nFor example, typing `{index + 1}` will return {matches[index]}'
            embed.add_field(name='Instructions', value=instructions)

            message = await self.send(embed=embed)
        else:
            await self.send('There are too many matches... Which one did you mean? **Only say the number**.')
            message = await self.send(entries)

        def check(m):
            return (m.author.id == self.author.id
                    and m.channel.id == self.channel.id
                    and m.content.isdigit())

        await self.release()

        # TODO: Support reactions again. This will take a ton of code to do properly though.
        try:
            for i in range(tries):
                try:
                    msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                except asyncio.TimeoutError:
                    raise ValueError('Took too long. Goodbye.')

                index = int(msg.content)
                try:
                    return matches[index - 1]
                except IndexError:
                    await self.send(f'Please give me a valid number. {tries - i - 1} tries remaining...')

            raise ValueError('Too many tries. Goodbye.')
        finally:
            await message.delete()
            await self.acquire()

    # Nommed from Danny again.
    async def ask_confirmation(self, message, *, timeout=60.0, delete_after=True, reacquire=True,
                               author_id=None, destination=None):
        """An interactive reaction confirmation dialog.

        Parameters
        -----------
        message: Union[str, discord.Embed]
            The message to show along with the prompt.
        timeout: float
            How long to wait before returning.
        delete_after: bool
            Whether to delete the confirmation message after we're done.
        reacquire: bool
            Whether to release the database connection and then acquire it
            again when we're done.
        author_id: Optional[int]
            The member who should respond to the prompt. Defaults to the author of the
            Context's message.
        destination: Optional[discord.abc.Messageable]
            Where the prompt should be sent. Defaults to the channel of the
            Context's message.

        Returns
        --------
        Optional[bool]
            ``True`` if explicit confirm,
            ``False`` if explicit deny,
            ``None`` if deny due to timeout
        """

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

        instructions = f'React with {confirm_emoji} to confirm or {deny_emoji} to deny\n'

        if isinstance(message, discord.Embed):
            message.add_field(name="Instructions", value=instructions, inline=False)
            msg = await destination.send(embed=message)
        else:
            message = f'{message}\n\n{instructions}'
            msg = await destination.send(message)

        author_id = author_id or self.author.id

        def check(emoji, message_id, channel_id, user_id):
            return (message_id == msg.id
                    and user_id == author_id
                    and is_valid_emoji(str(emoji)))

        for em in emojis:
            await msg.add_reaction(em)

        if reacquire:
            await self.release()

        try:
            emoji, *_, = await self.bot.wait_for('raw_reaction_add', check=check, timeout=timeout)
            return str(emoji) == str(confirm_emoji)
        finally:
            if reacquire:
                await self.acquire()

            if delete_after:
                await msg.delete()

    def can_use_chiaki_repo_emojis(self):
        """Return True if the bot can use emojis from the Chiaki Emoji Repository.

        This is used for Minesweeper and Sudoku.
        """
        return (self.bot.get_guild(CHIAKI_EMOJI_REPO_GUILD_ID) is not None
                and self.me.permissions_in(self.channel).external_emojis)

    def bot_missing_perms(self, missing_perms, *, action=None):
        """Send a message that the bot is missing permssions.

        If action is not specified the actions for each permissions are used.
        """
        if action is None:
            actions = (
                _DEFAULT_MISSING_PERMS_ACTIONS.get(p, p.replace('_', ' '))
                for p in missing_perms
            )
            action = human_join(actions, final='or')

        nice_perms = (
            perm.replace('_', ' ').replace('guild', 'server').title()
            for perm in missing_perms
        )

        message = (
            f"Hey hey, I don't have permissions to {action}. "
            f'Please check if I have {human_join(nice_perms)}.'
        )

        return self.send(message)

    def bot_has_permissions(self, **permissions):
        perms = self.channel.permissions_for(self.me)
        return all(getattr(perms, perm) == value for perm, value in permissions.items())

    bot_has_embed_links = functools.partialmethod(bot_has_permissions, embed_links=True)
