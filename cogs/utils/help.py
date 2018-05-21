import asyncio
import functools
import inspect
import itertools
import operator
import random
import sys
from collections import OrderedDict

import discord
from discord.ext import commands
from more_itertools import chunked, flatten, ilen, sliced, spy

from .commands import all_names, command_category, walk_parents
from .deprecated import DeprecatedCommand
from .examples import command_example
from .misc import maybe_awaitable
from .paginator import InteractiveSession, Paginator, trigger


def _unique(iterable):
    return list(OrderedDict.fromkeys(iterable))


def _has_subcommands(command):
    return isinstance(command, commands.GroupMixin)


def _all_checks(command):
    # The main command's checks will be run regardless of if it's a group
    # and if command.invoke_without_command is True
    yield from command.checks
    if not command.parent:
        return

    for parent in walk_parents(command.parent):
        if not parent.invoke_without_command:
            yield from parent.checks

def _make_command_requirements(command):
    requirements = []
    # All commands in this cog are owner-only anyway.
    if command.cog_name == 'Owner':
        requirements.append('**Bot Owner only**')

    def make_pretty(p):
        return p.replace('_', ' ').title().replace('Guild', 'Server')

    for check in _all_checks(command):
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
    name_docs = sorted((str(sub), sub.short_doc) for sub in set(_visible_sub_commands(command)))
    padding = max(len(name) for name, _ in name_docs)

    return '\n'.join(
        f'`{name:<{padding}}\u200b` \N{EM DASH} {doc}'
        for name, doc in name_docs
    )

def _visible_sub_commands(command):
    return (c for c in command.walk_commands() if not c.hidden and c.enabled)

def _at_least(iterable, n):
    return any(True for _ in itertools.islice(iterable, n, None))

def _requires_extra_page(command):
    return _has_subcommands(command) and _at_least(_visible_sub_commands(command), 4)


def _rreplace(s, old, new, occurrence=1):
    return new.join(s.rsplit(old, occurrence))


_example_key = '\N{INFORMATION SOURCE}'
_see_also_key = '\N{DOWNWARDS BLACK ARROW}'
SEE_EXAMPLE = f'For an example, click {_example_key}'
EXAMPLE_HEADER = '**Example**'


class HelpCommandPage(InteractiveSession):
    def __init__(self, ctx, command, func=None):
        super().__init__(ctx)
        self.command = command
        self.func = func
        self._toggle = False
        self._old_footer_text = None
        self._show_subcommands = False

        has_example, needs_see_also = False, True

        if not _requires_extra_page(command):
            needs_see_also = False
            self._show_subcommands = True

        self._example = ctx.bot.command_image_urls.get(command.qualified_name)
        if self._example:
            has_example = True

        self._reaction_map = self._reaction_maps[has_example, needs_see_also]

    @trigger(_example_key)
    async def show_example(self):
        self._toggle = toggle = not self._toggle
        current, func = self._current, self.func

        def swap_fields(direction):
            to_replace = (func(SEE_EXAMPLE), func(EXAMPLE_HEADER))[::direction]
            field = current.fields[-1]
            replaced = _rreplace(field.value, *to_replace)
            current.set_field_at(-1, name=field.name, value=replaced, inline=False)

        if toggle:
            current.set_image(url=self._example)
            swap_fields(1)
        else:
            if hasattr(current, '_image'):
                del current._image
                swap_fields(-1)
        return current

    @trigger(_see_also_key)
    def show_subcommands(self, embed=None):
        embed = embed or self._current
        command, func = self.command, self.func
        assert isinstance(command, commands.GroupMixin), "command has no subcommands"

        if self._show_subcommands:
            value = func(_list_subcommands_and_descriptions(command))
        else:
            value = func(f'Click {_see_also_key} to expand')

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

        cmd_name = f"`{clean_prefix}{command.full_parent_name} {' / '.join(all_names(command))}`"

        description = (command.help or '').format(prefix=clean_prefix)
        if isinstance(command, DeprecatedCommand):
            description = f'*{command.warning}*\n\n{description}'

        cmd_embed = discord.Embed(title=func(cmd_name), description=func(description), colour=self._bot.colour)

        requirements = _make_command_requirements(command)
        if requirements:
            cmd_embed.add_field(name=func("Requirements"), value=func(requirements))

        if _has_subcommands(command):
            self.show_subcommands(embed=cmd_embed)

        usage = command_example(command, ctx)
        if self._example:
            usage = f'{usage}\n\n{SEE_EXAMPLE}'
        else:
            usage = f'{usage}\n\nNo example for {command}... yet'

        cmd_embed.add_field(name=func("Usage"), value=func(usage), inline=False)

        # if usages is not None:
        #    cmd_embed.add_field(name=func("Usage"), value=func(usages), inline=False)
        category = command_category(command, 'Other')
        footer = f'Category: {category}'
        return cmd_embed.set_footer(text=func(footer))


def _without_keys(mapping, *keys):
    return OrderedDict((key, value) for key, value in mapping.items() if key not in keys)
_rmap_without = functools.partial(_without_keys, HelpCommandPage._reaction_map)

HelpCommandPage._reaction_maps = {
    (True, True): HelpCommandPage._reaction_map,
    (True, False): _rmap_without(_see_also_key),
    (False, True): _rmap_without(_example_key),
    (False, False): _rmap_without(_example_key, _see_also_key),
}
del _rmap_without, _without_keys


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


def _get_category_commands(bot, category):
    return {c for c in bot.all_commands.values() if command_category(c, 'other') == category}

class CogPages(Paginator):
    goto = None

    # Don't feel like doing an async def __init__ and hacking through that.
    # We have to make this async because we need to make the entries in one go.
    # As we have to check if the commands can be run, which entails querying the
    # DB too.
    @classmethod
    async def create(cls, ctx, category):
        command, commands = spy(
            c for c in _get_category_commands(ctx.bot, category)
            if not (c.hidden or ctx.bot.formatter.show_hidden)
        )

        pairs = [pair async for pair in _command_formatters(sorted(commands, key=str), ctx)]

        self = cls(ctx, _command_lines(pairs))
        pkg_name = command[0].module.rpartition('.')[0]
        module = sys.modules[pkg_name]
        self._cog_doc = inspect.getdoc(module) or 'No description... yet.'
        self._cog_name = category.title() or 'Other'

        return self

    def create_embed(self, entries):
        return (discord.Embed(colour=self.colour, description=self._cog_doc)
                .set_author(name=self._cog_name)
                .add_field(name='Commands', value='\n'.join(entries) + f'\n\n{CROSSED_NOTE}')
                .set_footer(text=f'Currently on page {self._index + 1}')
                )


# TODO: Save these images in the event of a deletion
CHIAKI_MOTIVATION_URL = 'http://pa1.narvii.com/6186/3d315c4d1d8f249a392fd7740c7004f28035aca9_hq.gif'
EASTER_EGG_COLOUR = 0xE91E63


class GeneralHelpPaginator(Paginator):
    first = None
    last = None
    help_page = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._do_easter_egg = False

    @classmethod
    async def create(cls, ctx):
        def sort_key(c):
            return command_category(c), c.qualified_name

        entries = (cmd for cmd in sorted(ctx.bot.commands, key=sort_key) if not cmd.hidden)

        nested_pages = []
        per_page = 30

        # (cog, description, first 10 commands)
        # (cog, description, next 10 commands)
        # ...
        for parent, cmds in itertools.groupby(entries, key=command_category):
            command, cmds = spy(cmds)
            command = next(iter(command))  # spy returns (list, iterator)

            # We can't rely on the package being in bot.extensions, because
            # maybe they wanted to only import one or a few extensions instead
            # of the whole folder.
            pkg_name = command.module.rpartition('.')[0]
            module = sys.modules[pkg_name]
            description = inspect.getdoc(module) or 'No description... yet.'

            lines = [pair async for pair in _command_formatters(cmds, ctx)]
            nested_pages.extend((parent.title(), description, page) for page in sliced(lines, per_page))

        self = cls(ctx, nested_pages, per_page=1)  # needed to break the slicing in __getitem__
        return self

    def create_embed(self, page):
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
                .set_footer(text=f'Page {self._index + 1}/{len(self._pages)}')
                )

    # These methods are overridden because docstrings are annoying

    @trigger('\N{BLACK LEFT-POINTING TRIANGLE}')
    def previous(self):
        """Back"""
        return super().previous() or (self.instructions() if self._index == 0 else None)

    @trigger('\N{BLACK RIGHT-POINTING TRIANGLE}')
    def next(self):
        """Next"""
        return super().next()

    @trigger('\N{INPUT SYMBOL FOR NUMBERS}')
    async def goto(self):
        """Goto"""
        return await maybe_awaitable(super().goto)

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
        pairs = list(cog_pages(flatten(self._pages), 1))
        padding = max(len(p[0]) for p in pairs)
        lines = [f'`\u200b{numbers:<{padding}}\u200b` - {name}' for numbers, name in pairs]
        print(lines)

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

    @trigger('\N{BLACK SQUARE FOR STOP}')
    async def stop(self):
        """Exit"""
        await super().stop()

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
            and reaction.emoji == '\N{HEAVY BLACK HEART}'
        ):
            return

        self._do_easter_egg = boo

    def check(self, reaction, user):
        if super().check(reaction, user):
            return True

        self._set_easter_egg(reaction, user, True)
