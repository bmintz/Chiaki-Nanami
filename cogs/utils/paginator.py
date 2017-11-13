import asyncio
import contextlib
import discord
import functools
import itertools
import logging
import random

from collections import OrderedDict
from discord.ext import commands

from .misc import maybe_awaitable


_log = logging.getLogger(__name__)


@contextlib.contextmanager
def _always_done_future(fut):
    fut = asyncio.ensure_future(fut)
    try:
        yield fut
    finally:
        if not fut.done():
            fut.cancel()


class DelimPaginator(commands.Paginator):
    def __init__(self, prefix='```', suffix='```', max_size=2000, join_delim='\n', **kwargs):
        super().__init__(prefix, suffix, max_size)
        self.escape_code = kwargs.get('escape_code', False)
        self.join_delim = join_delim

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, x):
        return self.pages[x]

    def add_line(self, line, escape_code=False):
        line = line.replace('`', '\u200b`') if self.escape_code else line
        super().add_line(line)

    def close_page(self):
        """Prematurely terminate a page."""
        self._current_page.append(self.suffix)
        prefix, *rest, suffix = self._current_page
        self._pages.append(f"{prefix}{self.join_delim.join(rest)}{suffix}")
        self._current_page = [self.prefix]
        self._count = len(self.prefix) + 1  # prefix + newline

    @classmethod
    def from_iterable(cls, iterable, **kwargs):
        paginator = cls(**kwargs)
        for i in iterable:
            paginator.add_line(i)
        return paginator

    @property
    def total_size(self):
        return sum(map(len, self))


# --------------------- Embed-related things ---------------------

def page(emoji):
    def decorator(func):
        func.__reaction_emoji__ = emoji
        return func
    return decorator


_extra_remarks = [
    'Does nothing',
    'Does absolutely nothing',
    'Still does nothing',
    'Really does nothing',
    'What did you expect',
    'Make Chiaki do a hula hoop',
    'Get slapped by Chiaki',
    'Hug Chiaki',
    ]


class BaseReactionPaginator:
    """Base class for all embed paginators.

    Subclasses must implement the default method with an emoji.
    Usage is something like this:

    class Paginator(BaseReactionPaginator):
        @page('\N{HEAVY BLACK HEART}')
        def default(self):
            return discord.Embed(description='hi myst \N{HEAVY BLACK HEART}')

        @page('\N{THINKING FACE}')
        def think(self):
            return discord.Embed(description='\N{THINKING FACE}')

    A page should either return a discord.Embed, or None if to indicate the
    page was invalid somehow. e.g. The page number given was out of bounds,
    or there were side effects associated with it.
    """

    def __init__(self, context, *, colour=None, color=None):
        if colour is None:
            colour = context.bot.colour if color is None else color

        self.colour = colour
        self.context = context
        self._paginating = True
        self._message = None
        self._current = None
        # in case a custom destination was specified, this is meant to be internal
        self._destination = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._reaction_map = OrderedDict()
        _suppressed_methods = set()
        # We can't use inspect.getmembers because it returns the members in
        # lexographical order, rather than definition order.
        for name, member in itertools.chain.from_iterable(b.__dict__.items() for b in cls.__mro__):
            if name.startswith('_'):
                continue

            # Support for using functools.partialmethod as a means of simplifying pages.
            is_callable = callable(member) or isinstance(member, functools.partialmethod)

            # Support suppressing page methods by assigning them to None
            if not (member is None or is_callable):
                continue

            # Let sub-classes override the current methods.
            if name in cls._reaction_map.values():
                continue

            # Let subclasses suppress page methods.
            if name in _suppressed_methods:
                continue

            if member is None:
                _suppressed_methods.add(name)
                continue

            emoji = getattr(member, '__reaction_emoji__', None)
            if emoji:
                cls._reaction_map[emoji] = name
                _log.debug('Initializing emoji %s for method %s in class %s',
                           hex(ord(emoji)), member.__qualname__, cls.__name__)

        # We need to move stop to the end (assuming it exists).
        # Otherwise it will show up somewhere in the middle
        with contextlib.suppress(StopIteration):
            key = next(k for k, v in cls._reaction_map.items() if v == 'stop')
            cls._reaction_map.move_to_end(key)

    def __len__(self):
        return len(self._reaction_map)

    def default(self):
        """The first page that will be shown.

        Subclasses must implement this.
        """
        raise NotImplementedError

    @property
    def color(self):
        return self.colour

    @color.setter
    def color(self, color):
        self.colour = color

    @page('\N{BLACK SQUARE FOR STOP}')
    def stop(self):
        """Stops the interactive pagination"""
        self._paginating = False

    def _check_reaction(self, reaction, user):
        return (reaction.message.id == self._message.id
                and user.id == self.context.author.id
                and reaction.emoji in self._reaction_map
                )

    async def add_buttons(self):
        for emoji in self._reaction_map:
            await self._message.add_reaction(emoji)

    async def on_only_one_page(self):
        # Override this if you need custom behaviour if there's only one page
        # If you would like stop pagination, simply call stop()
        await self._message.add_reaction(self.stop.__reaction_emoji__)

    async def interact(self, destination=None, *, timeout=120, delete_after=True):
        """Creates an interactive session."""
        ctx = self.context
        self._destination = destination = destination or ctx
        self._current = starting_embed = await maybe_awaitable(self.default)
        self._message = message = await destination.send(embed=starting_embed)

        def _put_reactions():
            # We need at least the stop button for killing the pagination
            # Otherwise it would kill the page immediately.
            coro = self.add_buttons() if len(self) > 1 else self.on_only_one_page()
            # allow us to react to reactions right away if we're paginating
            return asyncio.ensure_future(coro)

        try:
            future = _put_reactions()
            wait_for_reaction = functools.partial(ctx.bot.wait_for, 'reaction_add',
                                                  check=self._check_reaction, timeout=timeout)
            while self._paginating:
                try:
                    react, user = await wait_for_reaction()
                except asyncio.TimeoutError:
                    break
                else:
                    try:
                        attr = self._reaction_map[react.emoji]
                    except KeyError:
                        # Because subclasses *can* override the check we need to check
                        # that the check given is valid, ie that the check will return
                        # True if and only if the emoji is in the reaction map.
                        raise RuntimeError(f"{react.emoji} has no method attached to it, check "
                                           f"the {self._check_reaction.__qualname__} method")

                    next_embed = await maybe_awaitable(getattr(self, attr))
                    if next_embed is None:
                        continue

                    self._current = next_embed
                    with contextlib.suppress(discord.HTTPException):
                        # Manage Messages permissions is required to remove
                        # other people's reactions. Sometimes the bot doesn't
                        # have that for some reason. We must factor that in.
                        await message.remove_reaction(react.emoji, user)

                    try:
                        await message.edit(embed=next_embed)
                    except discord.NotFound:  # Message was deleted by someone else (somehow).
                        break
        finally:
            if not future.done():
                future.cancel()

            with contextlib.suppress(discord.HTTPException):
                if delete_after:
                    await message.delete()
                else:
                    await message.clear_reactions()

    @property
    def reaction_help(self):
        return '\n'.join(
            f'{em} => {getattr(self, f).__doc__}'
            for em, f in self._reaction_map.items()
        )


class ListPaginator(BaseReactionPaginator):
    def __init__(self, context, entries, *, title=discord.Embed.Empty,
                 color=None, colour=None, lines_per_page=15):
        super().__init__(context, colour=colour, color=color)
        self.entries = tuple(entries)
        self.per_page = lines_per_page
        self.title = title
        self._index = 0
        self._extra = set()

    def _check_reaction(self, reaction, user):
        return (super()._check_reaction(reaction, user)
                or (not self._extra.difference_update(self._reaction_map)
                and self._extra.add(reaction.emoji)))

    def _create_embed(self, idx, page):
        # Override this if you want paginated embeds
        # but you want to handle the pagination differently
        # Note that page is a list of entries (it's sliced)

        # XXX: Should this respect the embed description limit (2048 chars)?
        return (discord.Embed(title=self.title, colour=self.colour, description='\n'.join(page))
                .set_footer(text=f'Page: {idx + 1} / {len(self)} ({len(self.entries)} entries)')
                )

    def __getitem__(self, idx):
        if idx < 0:
            idx += len(self)

        self._index = idx
        base = idx * self.per_page
        page = self.entries[base:base + self.per_page]
        return self._create_embed(idx, page)

    def __len__(self):
        return -(-len(self.entries) // self.per_page)

    @page('\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}')
    def default(self):
        """Returns the first page"""
        return self[0]

    @page('\N{BLACK LEFT-POINTING TRIANGLE}')
    def previous(self):
        """Returns the previous page"""
        return self.page_at(self._index - 1)

    @page('\N{BLACK RIGHT-POINTING TRIANGLE}')
    def next(self):
        """Returns the next page"""
        return self.page_at(self._index + 1)

    @page('\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}')
    def last(self):
        """Returns the last page"""
        return self[-1]

    def page_at(self, index):
        """Returns a page given an index.

        Unlike __getitem__, this function does bounds checking and raises
        IndexError if the index is out of bounds.
        """
        if 0 <= index < len(self):
            return self[index]
        return None

    @page('\N{INPUT SYMBOL FOR NUMBERS}')
    async def numbered(self):
        """Takes a number from the user and goes to that page"""
        ctx = self.context
        channel = self._message.channel
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

        description = (
            f'Please enter a number from 1 to {len(self)}.\n\n'
            'You can also click the \N{INPUT SYMBOL FOR NUMBERS} to go back to where\n'
            'we left off, or any of the other reactions to\n'
            'navigate this paginator normally.'
        )

        embed = (discord.Embed(colour=self.colour, description=description)
                 .set_author(name=f'What page do you want to go to, {ctx.author.display_name}?')
                 .set_footer(text=f'We were on page {self._index + 1}')
                 )

        wait = functools.partial(asyncio.wait, timeout=60, return_when=asyncio.FIRST_COMPLETED)
        try:
            while True:
                # The three futures are such that so the user doesn't get "stuck" in the
                # number page. If they click on the number page by accident, then they
                # should have an easy way out.
                #
                # Thus we have to wait for three events:
                # 1. the reaction if the user really wants to go to a different page,
                # 2. the removal of the numbered reaction if the user wants to go back,m
                # 3. and the actual number of the page they want to go to.

                with _always_done_future(ctx.bot.wait_for('message', check=check)) as f1, \
                     _always_done_future(ctx.bot.wait_for('reaction_add', check=self._check_reaction)) as f2, \
                     _always_done_future(ctx.bot.wait_for('reaction_remove', check=remove_check)) as f3:
                    # ...
                    await self._message.edit(embed=embed)

                    done, pending = await wait((f1, f2, f3))
                    if not done:
                        # Effectively a timeout.
                        return self._current

                    result = await done.pop()

                    if isinstance(result, discord.Message):
                        # The user imputted a message, let's parse it normally.
                        to_delete.append(result)

                        result = int(result.content)
                        page = self.page_at(result - 1)
                        if page:
                            return page
                        else:
                            embed.description = f"That's not between 1 and {len(self)}..."
                            embed.colour = 0xf44336
                    else:
                        # The user probably added or removed a reaction.
                        react, user = result
                        if react.emoji == '\N{INPUT SYMBOL FOR NUMBERS}':
                            # User exited, go back to where we left off.
                            return self._current

                        # Treat it as if it was a normal reaction case.
                        try:
                            attr = self._reaction_map[react.emoji]
                        except KeyError:
                            # Because subclasses *can* override the check we need to check
                            # that the check given is valid, ie that the check will return
                            # True if and only if the emoji is in the reaction map.
                            raise RuntimeError(
                                f"{react.emoji} has no method attached to it, check "
                                f"the {self._check_reaction.__qualname__} method"
                            )

                        try:
                            return await maybe_awaitable(getattr(self, attr))
                        finally:
                            with contextlib.suppress(discord.HTTPException):
                                # Manage Messages permissions is required to remove
                                # other people's reactions. Sometimes the bot doesn't
                                # have that for some reason. We must factor that in.
                                await self._message.remove_reaction(react.emoji, user)
        finally:
            with contextlib.suppress(Exception):
                await channel.delete_messages(to_delete)

    @page('\N{INFORMATION SOURCE}')
    def help_page(self):
        """Shows this message"""
        initial_message = "This is the interactive help thing!",
        funcs = (f'{em} => {getattr(self, f).__doc__}' for em, f in self._reaction_map.items())
        extras = zip(self._extra, (random.choice(_extra_remarks) for _ in itertools.count()))
        remarks = itertools.starmap('{0} => {1}'.format, extras)

        joined = '\n'.join(itertools.chain(initial_message, funcs, remarks))

        return (discord.Embed(title=self.title, colour=self.colour, description=joined)
                .set_footer(text=f"From page {self._index + 1}")
                )

    async def add_buttons(self):
        fast_forwards = {'\U000023ed', '\U000023ee'}
        small = len(self) <= 3

        for emoji in self._reaction_map:
            # Gotta do this inefficient branch because of stop not being moved to
            # the end, so I can't just subract the two fast arrow emojis
            if not (small and emoji in fast_forwards):
                await self._message.add_reaction(emoji)

    async def interact(self, destination=None, *, timeout=120, delete_after=True):
        bot = self.context.bot
        with bot.temp_listener(self.on_reaction_remove):
            await super().interact(destination, timeout=timeout, delete_after=delete_after)

    async def on_reaction_remove(self, reaction, user):
        self._extra.discard(reaction.emoji)


class TitleBasedPages(ListPaginator):
    """Similar to ListPaginator, but takes a dict of title-content pages

    As a result, the content can easily exceed the limit of 2000 chars.
    Please use responsibly.
    """
    def __init__(self, context, entries, **kwargs):
        super().__init__(context, entries, **kwargs)
        self.entry_map = entries

    def _create_embed(self, idx, page):
        entry_title = self.entries[idx]
        return (discord.Embed(title=entry_title, colour=self.colour, description='\n'.join(page))
                .set_author(name=self.title)
                .set_footer(text=f'Page: {idx + 1} / {len(self)} ({len(self.entries)} entries)')
                )

    def __getitem__(self, idx):
        if idx < 0:
            idx += len(self)

        self._index = idx
        page = self.entry_map[self.entries[idx]]
        return self._create_embed(idx, page)

    def __len__(self):
        return len(self.entries)


class EmbedFieldPages(ListPaginator):
    """Similar to ListPaginator, but uses the fields instead of the description"""
    def __init__(self, context, entries, *,
                 description=discord.Embed.Empty, inline=True, **kwargs):
        super().__init__(context, entries, **kwargs)

        self.description = description
        self.inline = inline
        if self.per_page > 25:
            raise ValueError("too many fields per page (maximum 25)")

    def _create_embed(self, idx, page):
        embed = (discord.Embed(title=self.title, colour=self.colour, description=self.description)
                 .set_footer(text=f'Page: {idx + 1} / {len(self)} ({len(self.entries)} entries)')
                 )

        add_field = functools.partial(embed.add_field, inline=self.inline)
        for name, value in page:
            add_field(name=name, value=value)
        return embed
