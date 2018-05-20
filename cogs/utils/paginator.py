import asyncio
import collections
import contextlib
import functools
import itertools

import discord
from discord.ext import commands
from more_itertools import chunked, consume, iter_except, unique_everseen

from .misc import maybe_awaitable
from .queue import SimpleQueue


def trigger(emoji):
    """Add a function that will be called with a certain reaction"""
    def decorator(func):
        func.__reaction_emoji__ = emoji
        return func
    return decorator

page = trigger  # backwards compat

paginated = functools.partial(
    commands.bot_has_permissions,
    embed_links=True,
    add_reactions=True,
)

# Extract the predicate from the check...
_validate_context = paginated()(lambda: 0).__commands_checks__[0]


class _TriggerCooldown(commands.CooldownMapping):
    def __init__(self):
        super().__init__(commands.Cooldown(rate=5, per=2, type=commands.BucketType.user))

    def _bucket_key(self, tup):
        return tup

    def is_rate_limited(self, message_id, user_id):
        bucket = self.get_bucket((message_id, user_id))
        return bucket.update_rate_limit() is not None

_trigger_cooldown = _TriggerCooldown()


class InteractiveSession:
    """Base class for all interactive sessions.

    Subclasses must implement 'default' method. If necessary, they can
    override 'start'.

    Example:
    class Thing(InteractiveSession):
        @reaction('\N{HEAVY BLACK HEART}')
        def default(self):
            return discord.Embed(description='hi myst \N{HEAVY BLACK HEART}')

        @reaction('\N{THINKING FACE}')
        def think(self):
            return discord.Embed(description='\N{THINKING FACE}')

    A page should either return a discord.Embed, or None if to indicate the
    page was invalid somehow. e.g. The page number given was ofut of bounds,
    or there were side effects associated with it.
    """

    # TODO: Context-less __init__
    def __init__(self, ctx):
        self.context = ctx
        self._bot = ctx.bot
        self._channel = ctx.channel
        self._users = {ctx.author.id}
        self._message = None

        # XXX: Remove self._current
        self._current = None

        # Queue is used to avoid having reactions being ignored if they're
        # spammed or if a callback unexpectedly takes a long time.
        self._queue = SimpleQueue()

    def __init_subclass__(cls, *, stop_emoji='\N{BLACK SQUARE FOR STOP}', **kwargs):
        super().__init_subclass__(**kwargs)
        cls._reaction_map = callbacks = collections.OrderedDict()

        # Can't use inspect.getmembers for two reasons:
        # 1. It sorts the members lexographically, which is not what
        #    we want. We want them in definition order.
        # 2. It resolves descriptors too early. This causes things like
        #    x = page('emoji')(partialmethod(meth, ...)) to not be registered.
        #
        name_members = itertools.chain.from_iterable(b.__dict__.items() for b in cls.__mro__)
        for name, member in unique_everseen(name_members, key=lambda p: p[0]):
            emoji = getattr(member, '__reaction_emoji__', None)
            if emoji is None or emoji in callbacks:
                continue

            # Resolve any descriptors ahead of time so we can do _reaction_map[emoji](self)
            callbacks[emoji] = getattr(cls, name)

        if stop_emoji is not None and stop_emoji not in callbacks:
            callbacks[stop_emoji] = cls.stop

    def check(self, reaction, _):
        """Extra checks for reactions"""
        return reaction.emoji in self._reaction_map

    async def add_reactions(self):
        """Add the reactions to the message"""
        for emoji in self._reaction_map:
            await self._message.add_reaction(emoji)

    def default(self):
        """Return the first embed to start the controller"""
        raise NotImplementedError

    async def start(self):
        """First thing that gets called"""
        self._current = embed = await maybe_awaitable(self.default)
        self._message = await self._channel.send(embed=embed)

    async def stop(self):
        """Stop running the controller"""
        await self._queue.put(None)

    async def cleanup(self, *, delete_after):
        """Clean up anything else after stopping"""
        msg = self._message
        method = msg.delete if delete_after else msg.clear_reactions
        with contextlib.suppress(Exception):
            await method()

    # XXX: Remove release_connection
    async def run(self, *, timeout=120, delete_after=True, release_connection=True):
        """Run the interactive loop"""
        _validate_context(self.context)
        if release_connection:
            with contextlib.suppress(AttributeError):
                await self.context.release()

        await self.start()
        if self._message is None:
            # start() was overridden but no message was set
            raise RuntimeError('start() must set self._message')

        message = self._message
        task = self._bot.loop.create_task(self.add_reactions())

        async def on_reaction_add(reaction, user):
            if (
                reaction.message.id == message.id
                and user.id in self._users
                and self.check(reaction, user)
                and not _trigger_cooldown.is_rate_limited(message.id, user.id)
            ):
                # We must prepend the whole lot as we want to remove the reaction
                # *after* the callback was executed
                await self._queue.put((self._reaction_map[reaction.emoji], reaction.emoji, user))

        self._bot.add_listener(on_reaction_add)
        try:
            while True:
                # TODO: Would async_timeout be better here?
                try:
                    job = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break

                if job is None:
                    break

                callback, emoji, user = job

                result = await maybe_awaitable(callback, self)

                with contextlib.suppress(discord.HTTPException):
                    await message.remove_reaction(emoji, user)

                if result is None:
                    continue
                self._current = result  # backwards compat...

                try:
                    await message.edit(embed=result)
                except discord.NotFound:  # Message was deleted
                    break

        finally:
            self._bot.remove_listener(on_reaction_add)
            if not task.done():
                task.cancel()

            consume(iter_except(self._queue.get_nowait, asyncio.QueueEmpty))
            await self.cleanup(delete_after=delete_after)

    interact = run  # backwards compat

    @property
    def reaction_help(self):
        return '\n'.join(itertools.starmap('{0} => {1.__doc__}'.format, self._reaction_map.items()))


BaseReactionPaginator = InteractiveSession  # backwards compat

# ------------- Paginator --------------

class Paginator(InteractiveSession):
    """Class that takes an iterable of entries and paginates them.

    This is what often comes to mind when people talk about 'paginators'
    """
    def __init__(self, ctx, entries, *, per_page=15, title=discord.Embed.Empty, colour=None, **kwargs):
        super().__init__(ctx, **kwargs)
        self._pages = tuple(chunked(entries, per_page))
        self._index = 0

        if colour is None:
            colour = ctx.bot.colour

        # These should probably be removed at some point in the future.
        self.title = title
        self.colour = colour

    def single_page(self):
        """Return True if there is only one page, False otherwise"""
        return len(self._pages) == 1

    def small(self):
        """Return True if there are five pages or less, False otherwise"""
        return len(self._pages) <= 5

    async def start(self):
        await super().start()
        if self.single_page():
            # Don't even start paginating if there is only one page.
            await self.stop()

    async def cleanup(self, *, delete_after):
        if not self.single_page():
            await super().cleanup(delete_after=delete_after)

    async def add_reactions(self):
        if self.single_page():
            return

        fast_forwards = {'\U000023ed', '\U000023ee'}
        small = self.small()

        for emoji in self._reaction_map:
            if not (small and emoji in fast_forwards):
                await self._message.add_reaction(emoji)

    # Main methods
    def create_embed(self, page):
        """Create an embed given a slice of entries"""

        return (discord.Embed(title=self.title, colour=self.colour, description='\n'.join(page))
                .set_footer(text=f'Page: {self._index + 1} / {len(self._pages)} ({self.total} entries)')
                )

    def page_at(self, idx):
        """Return the embed that would be created at a certain point.

        None if the index is out of bounds.
        """
        if not 0 <= idx < len(self._pages):
            return None

        self._index = idx
        return self.create_embed(self._pages[idx])

    @trigger('\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}')
    def default(self):
        """First page"""
        return self.page_at(0)

    @trigger('\N{BLACK LEFT-POINTING TRIANGLE}')
    def previous(self):
        """Previous page"""
        return self.page_at(self._index - 1)

    @trigger('\N{BLACK RIGHT-POINTING TRIANGLE}')
    def next(self):
        """Next page"""
        return self.page_at(self._index + 1)

    @trigger('\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}')
    def last(self):
        """Last page"""
        return self.page_at(len(self._pages) - 1)

    @page('\N{INPUT SYMBOL FOR NUMBERS}')
    async def numbered(self):
        """Go to page"""
        ctx = self.context
        channel = self._message.channel
        create_task = asyncio.ensure_future
        wait_for = ctx.bot.wait_for

        to_delete = []

        def check(m):
            return (m.channel.id == channel.id
                    and m.author.id == ctx.author.id
                    and m.content.isdigit()
                    )

        def remove_check(reaction, user):
            return (reaction.message.id == self._message.id
                    and user.id == self.context.author.id
                    and reaction.emoji == '\N{INPUT SYMBOL FOR NUMBERS}')

        description = f'Please enter a number from 1 to {len(self._pages)}.'

        embed = (discord.Embed(colour=self.colour, description=description)
                 .set_author(name=f'What page do you want to go to, {ctx.author.display_name}?')
                 .set_footer(text=f'We were on page {self._index + 1}')
                 )

        # The three futures are such that so the user doesn't get "stuck" in the
        # number page. If they click on the number page by accident, then they
        # should have an easy way out.
        #
        # Thus we have to wait for three events:
        # 1. the reaction if the user really wants to go to a different page,
        # 2. the removal of the numbered reaction if the user wants to go back,m
        # 3. and the actual number of the page they want to go to
        event_checks = [
            ('message', check), ('reaction_add', self.check), ('reaction_remove', remove_check)
        ]
        futures = [create_task(wait_for(ev, check=check)) for ev, check in event_checks]

        def reset_future(fut):
            idx = futures.index(fut)
            ev, check = event_checks[idx]
            futures[idx] = create_task(wait_for(ev, check=check))

        go_back = f'To go back, click \N{INPUT SYMBOL FOR NUMBERS} again.\n'

        try:
            while True:
                embed.description += f'\n\n{go_back}'
                await self._message.edit(embed=embed)

                done, _ = await asyncio.wait(
                    futures,
                    timeout=60,
                    return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    # Effectively a timeout.
                    return self._current

                fut = done.pop()
                result = fut.result()

                if not isinstance(result, discord.Message):
                    # The user probably added or removed a reaction.
                    react, _ = result
                    if react.emoji == '\N{INPUT SYMBOL FOR NUMBERS}':
                        # User exited, go back to where we left off.
                        return self._current
                    return None
                else:
                    # The user imputted a message, let's parse it normally.
                    to_delete.append(result)

                    result = int(result.content)
                    page = self.page_at(result - 1)
                    if page:
                        return page
                    else:
                        reset_future(fut)
                        embed.description = f"That's not between 1 and {len(self._pages)}..."
                        embed.colour = 0xf44336
        finally:
            with contextlib.suppress(Exception):
                await channel.delete_messages(to_delete)

            for f in futures:
                if not f.done():
                    f.cancel()

    @property
    def total(self):
        """Return the total number of entries in the list"""
        return sum(map(len, self._pages))

ListPaginator = Paginator  # also backwards compat


# -------------- Field Pages ----------------------

class EmbedFieldPages(ListPaginator):
    """Similar to ListPaginator, but uses the fields instead of the description"""
    def __init__(self, context, entries, *, inline=True, **kwargs):
        super().__init__(context, entries, **kwargs)

        self.inline = inline
        if len(self._pages) > 25:
            raise ValueError("too many fields per page (maximum 25)")

    def create_embed(self, page):
        embed = (discord.Embed(title=self.title, colour=self.colour)
                 .set_footer(text=f'Page: {self._index + 1} / {len(self._pages)} ({self.total} entries)')
                 )

        add_field = functools.partial(embed.add_field, inline=self.inline)
        for name, value in page:
            add_field(name=name, value=value)
        return embed
