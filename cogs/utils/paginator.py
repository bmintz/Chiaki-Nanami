import asyncio
import contextlib
import discord
import functools
import itertools
import random

from collections import OrderedDict
from discord.ext import commands

from .misc import maybe_awaitable


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
        """Exit"""
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
        """First page"""
        return self[0]

    @page('\N{BLACK LEFT-POINTING TRIANGLE}')
    def previous(self):
        """Previous page"""
        return self.page_at(self._index - 1)

    @page('\N{BLACK RIGHT-POINTING TRIANGLE}')
    def next(self):
        """Next page"""
        return self.page_at(self._index + 1)

    @page('\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}')
    def last(self):
        """Last page"""
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
        """Go to page"""
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
        """Help - this message"""
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


# --------- Below is the Help paginator ----------

import inspect
import operator
import sys
import textwrap
import time

from more_itertools import ilen, sliced, spy

from .context_managers import temp_attr


def _unique(iterable):
    return list(OrderedDict.fromkeys(iterable))


def _all_names(command):
    return [command.name, *command.aliases]


def _has_subcommands(command):
    return isinstance(command, commands.GroupMixin)


def _command_category(command):
    instance = command.instance
    if instance is None:
        return '\u200b\u200bNone'

    category = instance.__class__.__parent_category__ or '\u200bOther'
    return category.title()


def _make_command_requirements(command):
    requirements = []
    # All commands in this cog are owner-only anyway.
    if command.cog_name == 'Owner':
        requirements.append('**Bot Owner only**')

    def make_pretty(p):
        return p.replace('_', ' ').title().replace('Guild', 'Server')

    for check in command.checks:
        name = getattr(check, '__qualname__', '')

        if name.startswith('is_owner'):
            # the bot owner line must come above every other line, for emphasis.
            requirements.insert(0, '**Bot Owner only**')
        elif name.startswith('has_permissions'):
            permissions = inspect.getclosurevars(check).nonlocals['perms']
            pretty_perms = [make_pretty(k) if v else f'~~{make_pretty(k)}~~'
                            for k, v in permissions.items()]

            perm_names = ', '.join(pretty_perms)
            requirements.append(f'{perm_names} permission{"s" * (len(pretty_perms) != 1)}')

    return '\n'.join(requirements)


class HelpCommandPage(BaseReactionPaginator):
    def __init__(self, ctx, command, func=None):
        super().__init__(ctx)
        self.command = command
        self.func = func
        self._toggle = True
        self._on_subcommand_page = False
        self._reaction_map = self._reaction_map if _has_subcommands(command) else self._normal_reaction_map

    @page('\N{INFORMATION SOURCE}')
    def default(self):
        if self._on_subcommand_page:
            self._on_subcommand_page = toggle = False
        else:
            self._toggle = toggle = not self._toggle

        meth = self._example if toggle else self._command_info
        return meth()

    @page('\N{DOWNWARDS BLACK ARROW}')
    def subcommands(self):
        if self._on_subcommand_page:
            return None

        ctx, command = self.context, self.command

        assert isinstance(command, commands.GroupMixin), "command has no subcommands"
        self._on_subcommand_page = True
        subs = sorted(map(str, set(command.walk_commands())))

        note = (
            f'Type `{ctx.clean_prefix}{ctx.invoked_with} {command} subcommand`'
            f' for more info on a subcommand.\n'
            f'(e.g. type `{ctx.clean_prefix}{ctx.invoked_with} {random.choice(subs)}`)'
        )

        return (discord.Embed(colour=self.colour, description='\n'.join(map('`{}`'.format, subs)))
                .set_author(name=f'Child Commands for {command}')
                .add_field(name='\u200b', value=note, inline=False)
                )

    def _command_info(self):
        command, ctx, func = self.command, self.context, self.func
        clean_prefix = ctx.clean_prefix
        # usages = self.command_usage

        # if usage is truthy, it will immediately return with that usage. We don't want that.
        with temp_attr(command, 'usage', None):
            signature = command.signature

        requirements = _make_command_requirements(command) or 'None'
        cmd_name = f"`{clean_prefix}{command.full_parent_name} {' / '.join(_all_names(command))}`"

        description = (command.help or '').format(prefix=clean_prefix)

        cmd_embed = (discord.Embed(title=func(cmd_name), description=func(description), colour=self.colour)
                     .add_field(name=func("Requirements"), value=func(requirements))
                     .add_field(name=func("Signature"), value=f'`{func(signature)}`', inline=False)
                     )

        if _has_subcommands(command):
            prompt = func('Click \N{DOWNWARDS BLACK ARROW} to see all the subcommands!')
            cmd_embed.add_field(name=func('Subcommands'), value=prompt, inline=False)

        # if usages is not None:
        #    cmd_embed.add_field(name=func("Usage"), value=func(usages), inline=False)
        category = _command_category(command)
        footer = f'Category: {category} | Click the info button below to see an example.'
        return cmd_embed.set_footer(text=func(footer))

    def _example(self):
        command, bot = self.command, self.context.bot

        embed = discord.Embed(colour=self.colour).set_author(name=f'Example for {command}')

        try:
            image_url = bot.command_image_urls[self.command.qualified_name]
        except (KeyError, AttributeError):
            embed.description = f"`{self.command}` doesn't have an image.\nContact MIkusaba#4553 to fix that!"
        else:
            embed.set_image(url=image_url)

        return embed.set_footer(text='Click the info button to go back.')


HelpCommandPage._normal_reaction_map = HelpCommandPage._reaction_map.copy()
del HelpCommandPage._normal_reaction_map['\N{DOWNWARDS BLACK ARROW}']


async def _can_run(command, ctx):
    try:
        return await command.can_run(ctx)
    except commands.CommandError:
        return False


async def _command_formatters(commands, ctx):
    for command in commands:
        fmt = '`{}`' if await _can_run(command, ctx) else '~~`{}`~~'
        yield map(fmt.format, _all_names(command))


CROSSED_NOTE = "**Note:** You can't use commands\nthat are ~~crossed out~~."


class CogPages(ListPaginator):
    numbered = None

    # Don't feel like doing an async def __init__ and hacking through that.
    # We have to make this async because we need to make the entries in one go.
    # As we have to check if the commands can be run, which entails querying the
    # DB too.
    @classmethod
    async def create(cls, ctx, cog):
        cog_name = cog.__class__.__name__
        entries = (c for c in ctx.bot.get_cog_commands(cog_name)
                   if not (c.hidden or ctx.bot.formatter.show_hidden))

        formats = _command_formatters(sorted(entries, key=str), ctx)
        lines = [' | '.join(line) async for line in formats]

        self = cls(ctx, lines)
        self._cog_doc = inspect.getdoc(cog) or 'No description... yet.'
        self._cog_name = cog_name

        return self

    def _create_embed(self, idx, entries):
        return (discord.Embed(colour=self.colour, description=self._cog_doc)
                .set_author(name=self._cog_name)
                .add_field(name='Commands', value='\n'.join(entries) + f'\n\n{CROSSED_NOTE}')
                .set_footer(text=f'Currently on page {idx + 1}')
                )


# TODO: Save these images in the event of a deletion
CHIAKI_INTRO_URL = 'https://66.media.tumblr.com/feb7b9be75025afadd5d03fe7ad63aba/tumblr_oapg2wRooV1vn8rbao10_r2_500.gif'
CHIAKI_MOTIVATION_URL = 'http://pa1.narvii.com/6186/3d315c4d1d8f249a392fd7740c7004f28035aca9_hq.gif'


class GeneralHelpPaginator(ListPaginator):
    help_page = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._start_time = None
        self._index = -1

    @classmethod
    async def create(cls, ctx):
        def sort_key(c):
            return _command_category(c), c.qualified_name

        entries = (
            cmd for cmd in sorted(ctx.bot.commands, key=sort_key)
            if not (cmd.hidden or cmd.instance.__hidden__)
        )

        nested_pages = []
        per_page = 10

        # (cog, description, first 10 commands)
        # (cog, description, next 10 commands)
        # ...
        for parent, cmds in itertools.groupby(entries, key=_command_category):
            command, cmds = spy(cmds)
            command = next(iter(command))  # spy returns (list, iterator)

            # We can't rely on the package being in bot.extensions, because
            # maybe they wanted to only import one or a few extensions instead
            # of the whole folder.
            pkg_name = command.module.rpartition('.')[0]
            module = sys.modules[pkg_name]
            description = inspect.getdoc(module) or 'No description... yet.'

            lines = [' | '.join(line) async for line in _command_formatters(cmds, ctx)]
            nested_pages.extend((parent, description, page) for page in sliced(lines, per_page))

        self = cls(ctx, nested_pages, lines_per_page=1)  # needed to break the slicing in __getitem__
        return self

    def __len__(self):
        return self._num_extra_pages + super().__len__()

    def __getitem__(self, idx):
        if 0 <= idx < self._num_extra_pages:
            self._index = idx
            return self._page_footer_embed(self._extra_pages[idx](self))

        result = super().__getitem__(idx - self._num_extra_pages)
        # Properly set the index, because ListPagination.__getitem__ sets
        # _index to two pages before, breaking it
        self._index = idx
        return result

    def _page_footer_embed(self, embed, *, offset=0):
        return embed.set_footer(text=f'Page {self._index + offset + 1}/{len(self)}')

    def _create_embed(self, idx, page):
        name, description, lines = page[0]
        note = f'For more help on a command,\ntype `{self.context.clean_prefix}help "a command"`.'
        # ZWS is needed for mobile where they like to strip blank lines for no reason.
        commands = '\n'.join(lines) + f'\n{"-" * 30}\n{note}\n\u200b\n{CROSSED_NOTE}'

        return self._page_footer_embed(
            discord.Embed(colour=self.colour, description=description)
            .set_author(name=name)
            .add_field(name='Commands', value=commands),
            offset=self._num_extra_pages
        )

    def intro(self):
        """The intro, ie the thing you just saw."""
        ctx = self.context
        bot = ctx.bot
        instructions = (
            'To see all the categories, click \N{BLACK RIGHT-POINTING TRIANGLE}.\n'
            f'For more help, go to the **[support server]({bot.support_invite})**'
        )

        return (discord.Embed(colour=self.colour, description=instructions)
                .set_author(name=f"\u2764 Hi, {ctx.author.display_name}. Welcome to Chiaki Help!")
                .set_image(url=CHIAKI_INTRO_URL)
                )

    # Needed to table_of_contents will set the _index properly
    def first(self):
        """Table of contents"""
        return self[0]

    def default(self):
        # Delete the first page so the Table of Contents will be the first page.
        # XXX: Deal with pressing the previous page button
        self.default = self.first
        return self.intro()

    def instructions(self):
        """Instructions"""
        description = (
            f'**Click one of the reactions below**\n'
            '-------------------------------------\n'
            + self.reaction_help
        )
        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name='Instructions')
                )

    def table_of_contents(self):
        """Table of Contents"""
        bot = self.context.bot
        extra_docs = enumerate(map(inspect.getdoc, self._extra_pages), start=1)
        extra_lines = itertools.starmap('`{0}` - {1}'.format, extra_docs)

        def cog_pages(iterable, start):
            for name, g in itertools.groupby(iterable, key=operator.itemgetter(0)):
                count = ilen(g)
                if count == 1:
                    yield str(start), name
                else:
                    yield f'{start}-{start + count - 1}', name
                start += count

        # create the page numbers for the cogs
        pairs = list(cog_pages(self.entries, self._num_extra_pages + 1))
        padding = max(len(p[0]) for p in pairs)
        lines = (f'`\u200b{numbers:<{padding}}\u200b` - {name}' for numbers, name in pairs)

        description = f'For more help, go to the **[support server]({bot.support_invite})**'
        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name='Help', icon_url=self.context.bot.user.avatar_url)
                .add_field(name='Table of Contents', value='\n'.join(extra_lines))
                .add_field(name='Categories', value='\n'.join(lines), inline=False)
                )

    def how_to_use(self):
        """How to use the bot"""
        description = (
            'The signature is actually pretty simple!\n'
            "It's always there in the \"Signature\" field when\n"
            f'you do `{self.context.clean_prefix} help command`.'
        )

        note = textwrap.dedent('''
            **Don't type in the brackets!**
            --------------------------------
            This means you must type the commands like this:
            YES: `->inrole My Role`
            NO: `->inrole <My Role>` 
            (unless your role is actually named "<My Role>"...)
        ''')

        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name='So... how do I use this bot?')
                .add_field(name='<argument>', value='The argument is **required**. \nYou must specify this.', inline=False)
                .add_field(name='[argument]', value="The argument is **optional**. \nYou don't have to specify this..", inline=False)
                .add_field(name='[A|B]', value='You can type either **A** or **B**.', inline=False)
                .add_field(name='[arguments...]', value='You can have multiple arguments.', inline=False)
                .add_field(name='Note', value=note, inline=False)
                )

    @page('\N{BLACK SQUARE FOR STOP}')
    async def stop(self):
        """Exit"""
        super().stop()

        # Only do it for a minute, so if someone does a quick stop,
        # we'll grant them their wish of stopping early.
        end = time.monotonic()
        if end - self._start_time < 60:
            return

        final_embed = (discord.Embed(colour=self.colour, description='*Remember...* \N{HEAVY BLACK HEART}')
                       .set_author(name='Thank you for looking at the help page!')
                       .set_image(url=CHIAKI_MOTIVATION_URL)
                       )

        # haaaaaaaaaaaack
        await self._message.edit(embed=final_embed)
        return await asyncio.sleep(10)

    _extra_pages = [
        table_of_contents,
        instructions,
    ]
    _num_extra_pages = len(_extra_pages)

    @page('\N{WHITE QUESTION MARK ORNAMENT}')
    def signature(self):
        """How to use the bot"""
        return self.how_to_use()

    async def interact(self, **kwargs):
        self._start_time = time.monotonic()
        await super().interact(**kwargs)


rmap = GeneralHelpPaginator._reaction_map
# signature is currently at the beginning so we need to move it to the end
rmap.move_to_end('\N{WHITE QUESTION MARK ORNAMENT}')
del rmap
