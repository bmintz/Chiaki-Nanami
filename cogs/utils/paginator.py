import asyncio
import contextlib
import discord
import functools
import inspect
import itertools
import random

from collections import OrderedDict
from discord.ext import commands
from more_itertools import unique_everseen

from .misc import maybe_awaitable


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


paginated = functools.partial(
    commands.bot_has_permissions,
    embed_links=True,
    add_reactions=True,
)

# Extract the predicate from the check...
_validate_context = paginated()(lambda: 0).__commands_checks__[0]


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

    def __init_subclass__(cls, *, stop='\N{BLACK SQUARE FOR STOP}', **kwargs):
        super().__init_subclass__(**kwargs)
        cls._reaction_map = callbacks = OrderedDict()

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

        # Some people might want to move the stop emoji
        # or have already defined stop as a button somewhere else
        if stop is not None:
            if stop in callbacks:
                callbacks.move_to_end(stop)
            else:
                cls.stop.__reaction_emoji__ = stop
                callbacks[stop] = cls.stop

    def __len__(self):
        return len(self._reaction_map)

    def default(self):
        """The first page that will be shown.

        Subclasses must implement this.
        """
        raise NotImplementedError

    def check(self, reaction, user):
        """The check that will be used to see if a reaction is valid

        Subclasses may override this if needed.
        """
        return (reaction.message.id == self._message.id
                and user.id == self.context.author.id
                and reaction.emoji in self._reaction_map
                )

    def stop(self):
        """Exit"""
        self._paginating = False

    @property
    def color(self):
        return self.colour

    @color.setter
    def color(self, color):
        self.colour = color

    async def add_buttons(self):
        for emoji in self._reaction_map:
            await self._message.add_reaction(emoji)

    async def on_only_one_page(self):
        # Override this if you need custom behaviour if there's only one page
        # If you would like stop pagination, simply call stop()
        await self._message.add_reaction(self.stop.__reaction_emoji__)

    async def interact(self, *, timeout=120, delete_after=True, release_connection=True):
        """Creates an interactive session."""
        ctx = self.context
        _validate_context(ctx)

        if release_connection:
            with contextlib.suppress(AttributeError):
                await ctx.release()

        self._current = starting_embed = await maybe_awaitable(self.default)
        self._message = message = await ctx.send(embed=starting_embed)

        def _put_reactions():
            # We need at least the stop button for killing the pagination
            # Otherwise it would kill the page immediately.
            coro = self.add_buttons() if len(self) > 1 else self.on_only_one_page()
            # allow us to react to reactions right away if we're paginating
            return asyncio.ensure_future(coro)

        try:
            future = _put_reactions()
            wait_for_reaction = functools.partial(ctx.bot.wait_for, 'reaction_add',
                                                  check=self.check, timeout=timeout)
            while self._paginating:
                try:
                    react, user = await wait_for_reaction()
                except asyncio.TimeoutError:
                    break
                else:
                    try:
                        func = self._reaction_map[react.emoji]
                    except KeyError:
                        # Because subclasses *can* override the check we need to check
                        # that the check given is valid, ie that the check will return
                        # True if and only if the emoji is in the reaction map.
                        raise RuntimeError(f"{react.emoji} has no method attached to it, check "
                                           f"the {self.check.__qualname__} method")

                    next_embed = await maybe_awaitable(func, self)
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
        return '\n'.join(itertools.starmap('{0} => {1.__doc__}'.format, self._reaction_map.items()))


class ListPaginator(BaseReactionPaginator):
    def __init__(self, context, entries, *, title=discord.Embed.Empty,
                 color=None, colour=None, lines_per_page=15):
        super().__init__(context, colour=colour, color=color)
        self.entries = tuple(entries)
        self.per_page = lines_per_page
        self.title = title
        self._index = 0
        self._extra = set()

    def check(self, reaction, user):
        if not (reaction.message.id == self._message.id and user.id == self.context.author.id):
            return

        if str(reaction.emoji) in self._reaction_map:
            return True

        self._extra.add(reaction.emoji)

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

        description = f'Please enter a number from 1 to {len(self)}.'

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

                done, pending = await asyncio.wait(
                    futures,
                    timeout=60,
                    return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    # Effectively a timeout.
                    return self._current

                fut = done.pop()
                result = fut.result()

                if isinstance(result, discord.Message):
                    # The user imputted a message, let's parse it normally.
                    to_delete.append(result)

                    result = int(result.content)
                    page = self.page_at(result - 1)
                    if page:
                        return page
                    else:
                        reset_future(fut)
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
                        func = self._reaction_map[react.emoji]
                    except KeyError:
                        # Because subclasses *can* override the check we need to check
                        # that the check given is valid, ie that the check will return
                        # True if and only if the emoji is in the reaction map.
                        raise RuntimeError(
                            f"{react.emoji} has no method attached to it, check "
                            f"the {self.check.__qualname__} method"
                        )

                    try:
                        return await maybe_awaitable(func, self)
                    finally:
                        with contextlib.suppress(discord.HTTPException):
                            # Manage Messages permissions is required to remove
                            # other people's reactions. Sometimes the bot doesn't
                            # have that for some reason. We must factor that in.
                            await self._message.remove_reaction(react.emoji, user)
        finally:
            with contextlib.suppress(Exception):
                await channel.delete_messages(to_delete)

            for f in futures:
                if not f.done():
                    f.cancel()

    @page('\N{INFORMATION SOURCE}')
    def help_page(self):
        """Help - this message"""
        initial_message = "This is the interactive help thing!",
        docs = itertools.starmap('{0} => {1.__doc__}'.format, self._reaction_map.items())
        extras = zip(self._extra, (random.choice(_extra_remarks) for _ in itertools.count()))
        remarks = itertools.starmap('{0} => {1}'.format, extras)

        joined = '\n'.join(itertools.chain(initial_message, docs, remarks))

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

    async def interact(self, **kwargs):
        bot = self.context.bot
        with bot.temp_listener(self.on_reaction_remove):
            await super().interact(**kwargs)

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

from more_itertools import chunked, ilen, sliced, spy

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

def _list_subcommands_and_descriptions(command):
    name_docs = sorted((str(sub), sub.short_doc) for sub in set(command.walk_commands()))
    padding = max(len(name) for name, _ in name_docs)

    return '\n'.join(
        f'`{name:<{padding}}\u200b` \N{EM DASH} {doc}'
        for name, doc in name_docs
    )

def _at_least(iterable, n):
    return any(True for _ in itertools.islice(iterable, n, None))

def _requires_extra_page(command):
    return _has_subcommands(command) and _at_least(command.walk_commands(), 6)


class HelpCommandPage(BaseReactionPaginator):
    def __init__(self, ctx, command, func=None):
        super().__init__(ctx)
        self.command = command
        self.func = func
        self._toggle = False
        self._old_footer_text = None
        self._show_subcommands = False

        if not _requires_extra_page(command):
            self._reaction_map = self._normal_reaction_map
            self._show_subcommands = True

    @page('\N{INFORMATION SOURCE}')
    async def show_example(self):
        self._toggle = toggle = not self._toggle

        command, bot, current = self.command, self.context.bot, self._current
        if toggle:
            image_url = bot.command_image_urls.get(command.qualified_name)
            if not image_url:
                return None
            current.set_image(url=image_url)
            current.add_field(name='\u200b', value='**Example**', inline=False)
        else:
            if hasattr(current, '_image'):
                del current._image
                current.remove_field(-1)
        return current

    @page('\N{DOWNWARDS BLACK ARROW}')
    def show_subcommands(self, embed=None):
        embed = embed or self._current
        command, func = self.command, self.func
        assert isinstance(command, commands.GroupMixin), "command has no subcommands"

        if self._show_subcommands:
            value = func(_list_subcommands_and_descriptions(command))
        else:
            value = func('Click \N{DOWNWARDS BLACK ARROW} to see all the subcommands!')

        self._show_subcommands = not self._show_subcommands

        see_also = func('See also')
        field_index = discord.utils.find(
            lambda idx_field: idx_field[1].name == see_also,
            enumerate(embed.fields)
        )

        if field_index is not None:
            return embed.set_field_at(field_index[0], name=see_also, value=value, inline=False)
        else:
            return embed.add_field(name=see_also, value=value, inline=False)

    def default(self):
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
            self.show_subcommands(embed=cmd_embed)

        # if usages is not None:
        #    cmd_embed.add_field(name=func("Usage"), value=func(usages), inline=False)
        category = _command_category(command)
        footer = f'Category: {category} | Click the info button below to see an example.'
        return cmd_embed.set_footer(text=func(footer))


HelpCommandPage._normal_reaction_map = HelpCommandPage._reaction_map.copy()
del HelpCommandPage._normal_reaction_map['\N{DOWNWARDS BLACK ARROW}']


async def _can_run(command, ctx):
    try:
        return await command.can_run(ctx)
    except commands.CommandError:
        return False


async def _command_formatters(commands, ctx):
    for command in commands:
        # Can't do async-genexpr because Python <3.6.4 requires the function
        # to be async def, however then we'd have to await it.
        yield command.name, await _can_run(command, ctx)


NUM_COMMAND_COLUMNS = 2
def _command_lines(command_can_run_pairs):
    if len(command_can_run_pairs) % 2:
        # Avoid modifying the list if we can help it
        command_can_run_pairs = command_can_run_pairs + [('', '')]

    pairs = list(sliced(command_can_run_pairs, NUM_COMMAND_COLUMNS))
    widths = [max(len(c[0]) for c in column) for column in zip(*pairs)]

    # XXX: Does not work on iOS clients for some reason -- the
    #      strikethrough doesn't render at all.
    def format_pair(pair, width):
        command, can_run = pair
        if not command:
            return ''

        # Simply doing f'`{command:>width + 1}`' is not enough because
        # we want to cross out only the command text, not the entire
        # block. Doing that requires making two code blocks, one for
        # the command, and one padded with spaces.
        #
        # However Discord loves to be really fucky with codeblocks. If
        # there are two backticks close together, it will make one huge
        # code block with the middle two unescaped. e.g `abc``test` will
        # make one long code block with the string of "abc``test" rather
        # than what we really want.
        #
        # Discord also loves to mess around with spaces, because our code
        # block is technically empty, Discord will just strip out the
        # whitespace, leaving us with an empty code block. Thus we need
        # three zwses -- one between the two code blocks to prevent them
        # from merging, and two at each end of the 2nd code block to prevent
        # the padding spaces from getting stripped.
        #
        # ~~*phew*~~
        formatted = f'`{command}`'
        if not can_run:
            formatted = f'~~{formatted}~~'

        to_pad = width - len(command) + 1
        padding = f'\u200b`\u200b{" " * to_pad}\u200b`' if to_pad > 0 else ''

        return formatted + padding

    return (' '.join(map(format_pair, pair, widths)) for pair in pairs)


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

        pairs = [pair async for pair in _command_formatters(sorted(entries, key=str), ctx)]

        self = cls(ctx, _command_lines(pairs))
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
CHIAKI_MOTIVATION_URL = 'http://pa1.narvii.com/6186/3d315c4d1d8f249a392fd7740c7004f28035aca9_hq.gif'
EASTER_EGG_COLOUR = 0xE91E63


class GeneralHelpPaginator(ListPaginator):
    first = None
    last = None
    help_page = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._do_easter_egg = False

    @classmethod
    async def create(cls, ctx):
        def sort_key(c):
            return _command_category(c), c.qualified_name

        entries = (
            cmd for cmd in sorted(ctx.bot.commands, key=sort_key)
            if not (cmd.hidden or cmd.instance.__hidden__)
        )

        nested_pages = []
        per_page = 30

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

            lines = [pair async for pair in _command_formatters(cmds, ctx)]
            nested_pages.extend((parent, description, page) for page in sliced(lines, per_page))

        self = cls(ctx, nested_pages, lines_per_page=1)  # needed to break the slicing in __getitem__
        return self

    def _create_embed(self, idx, page):
        name, description, lines = page[0]
        random_command = random.choice(next(zip(*lines)))
        note = (
            f'For more help on a command,\ntype `{self.context.clean_prefix}help "a command"`.\n'
            f'**Example:** `{self.context.clean_prefix}help {random_command}`'
        )
        formatted = '\n'.join(_command_lines(lines))
        # ZWS is needed for mobile where they like to strip blank lines for no reason.
        commands = f'{formatted}\n{"-" * 30}\n{note}\n\u200b\n{CROSSED_NOTE}'

        if self._do_easter_egg:
            colour = EASTER_EGG_COLOUR
            description = f'\N{HEAVY BLACK HEART}{description}\N{HEAVY BLACK HEART}'
        else:
            colour = self.colour

        return (discord.Embed(colour=colour, description=description)
                .set_author(name=name)
                .add_field(name='Commands', value=commands)
                .set_footer(text=f'Page {self._index + 1}/{len(self)}')
                )

    # These methods are overridden because docstrings are annoying

    @page('\N{BLACK LEFT-POINTING TRIANGLE}')
    def previous(self):
        """Back"""
        return super().previous() or (self.instructions() if self._index == 0 else None)

    @page('\N{BLACK RIGHT-POINTING TRIANGLE}')
    def next(self):
        """Next"""
        return super().next()

    @page('\N{INPUT SYMBOL FOR NUMBERS}')
    async def numbered(self):
        """Goto"""
        return await maybe_awaitable(super().numbered)

    # End of this overriding silliness

    def instructions(self):
        """Table of Contents"""
        self._index = -1
        ctx = self.context
        bot = self.context.bot

        def cog_pages(iterable, start):
            for name, g in itertools.groupby(iterable, key=operator.itemgetter(0)):
                count = ilen(g)
                if count == 1:
                    yield str(start), name
                else:
                    yield f'{start}-{start + count - 1}', name
                start += count

        # create the page numbers for the cogs
        pairs = list(cog_pages(self.entries, 1))
        padding = max(len(p[0]) for p in pairs)
        lines = (f'`\u200b{numbers:<{padding}}\u200b` - {name}' for numbers, name in pairs)

        # create the compacted controls field
        emoji_docs = ((emoji, func.__doc__) for emoji, func in self._reaction_map.items())
        controls = '\n'.join(
            f'{p1[0]} `{p1[1]}` | `{p2[1]}` {p2[0]}'
            for p1, p2 in chunked(emoji_docs, 2)
        )

        description = (
            f'For help on a command, type `{ctx.clean_prefix}help command`.\n'
            f'For more help, go to the **[support server]({bot.support_invite})**'
        )

        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name='Chiaki Nanami Help', icon_url=bot.user.avatar_url)
                .add_field(name='Categories', value='\n'.join(lines), inline=False)
                .add_field(name='Controls', value=controls, inline=False)
                .set_footer(text='Click one of the reactions below <3')
                )

    default = instructions

    @page('\N{BLACK SQUARE FOR STOP}')
    async def stop(self):
        """Exit"""
        super().stop()

        if not self._do_easter_egg:
            return

        final_embed = (discord.Embed(colour=self.colour, description='*Remember...* \N{HEAVY BLACK HEART}')
                       .set_author(name='Thank you for looking at the help page!')
                       .set_image(url=CHIAKI_MOTIVATION_URL)
                       )

        # haaaaaaaaaaaack
        await self._message.edit(embed=final_embed)
        return await asyncio.sleep(10)

    # Stuff that affects the easter egg flag
    async def on_reaction_remove(self, reaction, user):
        self._set_easter_egg(reaction, user, False)

    def _set_easter_egg(self, reaction, user, boo):
        if not (
            reaction.message.id == self._message.id
            and user.id == self.context.author.id
            and reaction.emoji in '\N{HEAVY BLACK HEART}'
        ):
            return

        self._do_easter_egg = boo

    def check(self, reaction, user):
        if super().check(reaction, user):
            return True

        self._set_easter_egg(reaction, user, True)
