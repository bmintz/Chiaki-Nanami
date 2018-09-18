import asyncio
import copy
import inspect
import itertools
import operator
import random
import sys

import discord
from discord.ext import commands
from more_itertools import chunked, flatten, run_length, sliced, spy

from .commands import all_names, command_category, walk_parents
from .converter import BotCommand
from .deprecated import DeprecatedCommand
from .examples import command_example
from .misc import maybe_awaitable
from .paginator import Paginator, trigger


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

def _help_command_embed(ctx, command, func):
    clean_prefix = ctx.clean_prefix
    # usages = self.command_usage

    title = f"`{clean_prefix}{command.full_parent_name} {' / '.join(all_names(command))}`"

    description = (command.help or '').format(prefix=clean_prefix)
    if isinstance(command, DeprecatedCommand):
        description = f'*{command.warning}*\n\n{description}'

    embed = discord.Embed(title=func(title), description=func(description), colour=ctx.bot.colour)

    requirements = _make_command_requirements(command)
    if requirements:
        embed.add_field(name=func("Requirements"), value=func(requirements))

    usage = command_example(command, ctx)
    embed.add_field(name=func("Usage"), value=func(usage), inline=False)

    if _has_subcommands(command):
        subs = _list_subcommands_and_descriptions(command)
        embed.add_field(name='See also', value=subs, inline=False)

    category = command_category(command, 'Other')
    footer = f'Category: {category.title()}'
    return embed.set_footer(text=func(footer))


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

        # Discord ruined my ZWS hack. Excess whitespace, for some reason, gets
        # chopped off which makes padding on monospace nigh-on impossible.
        padding = " \u200b" * (width - len(command) + 1)
        formatted = f'`{command}{padding}`'
        return formatted if can_run else f'~~{formatted}~~'

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

    @trigger('\N{BLACK LEFT-POINTING TRIANGLE}', fallback=r'\<')
    def previous(self):
        """Back"""
        return super().previous() or (self.instructions() if self._index == 0 else None)

    @trigger('\N{BLACK RIGHT-POINTING TRIANGLE}', fallback=r'\>')
    def next(self):
        """Next"""
        return super().next()

    @trigger('\N{INPUT SYMBOL FOR NUMBERS}', block=True)
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
            for name, count in run_length.encode(map(operator.itemgetter(0), iterable)):
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

        # XXX: Should probably make a method for this.
        if self.using_reactions():
            docs = ((emoji, func.__doc__) for emoji, func in self._reaction_map.items())
            controls = '\n'.join(
                f'{p1[0]} `{p1[1]}` | `{p2[1]}` {p2[0]}'
                for p1, p2 in chunked(docs, 2)
            )
            footer_text = 'Click one of the reactions below <3'
        else:
            controls = '\n'.join(
                # "f-string expression cannot include a backslash"
                f'`' + pattern.replace('\\', '') + f'` = `{func.__doc__}`'
                for pattern, func in self._message_fallbacks
            )
            footer_text = 'Type one of these below <3'

        description = (
            f'For help on a command, type `{ctx.clean_prefix}help command`.\n'
            f'For more help, go to the **[support server]({bot.support_invite})**'
        )

        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name='Chiaki Nanami Help', icon_url=bot.user.avatar_url)
                .add_field(name='Categories', value='\n'.join(lines), inline=False)
                .add_field(name='Controls', value=controls, inline=False)
                .set_footer(text=footer_text)
                )

    default = instructions

    @trigger('\N{BLACK SQUARE FOR STOP}', fallback='exit')
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


# --------------- Main help command ----------------

class _HelpCommand(BotCommand):
    _choices = [
        'Help yourself.',
        'https://cdn.discordapp.com/attachments/329401961688596481/366323237526831135/retro.jpg',
        'Just remember the mitochondria is the powerhouse of the cell! \U0001f605',
        'Save me!',
    ]

    async def convert(self, ctx, arg):
        try:
            return await super().convert(ctx, arg)
        except commands.BadArgument:
            if arg.lower() != 'me':
                raise
            raise commands.BadArgument(random.choice(self._choices))


async def _help_command(ctx, command, func):
    permissions = ctx.me.permissions_in(ctx.channel)
    if not permissions.send_messages:
        # It's muted, there's no point in either sending the embed or DMing, cuz
        # it was probably muted for a good reason.
        return

    if permissions.embed_links:
        await ctx.send(embed=_help_command_embed(ctx, command, func))
        return
    
    try:
        await ctx.author.send(embed=_help_command_embed(ctx, command, func))
    except discord.Forbidden:
        # We can't embed but if we couldn't send then this part of the code would
        # never be run anyways so we can just make it respond.
        await ctx.send(
            "Sorry... couldn't send you the help, either turn on your DMs or "
            "let me send embeds."
        )

async def _help(ctx, command=None, func=lambda s: s):
    if command is not None:
        await _help_command(ctx, command, func)
        return

    paginator = await GeneralHelpPaginator.create(ctx)
    try:
        await paginator.interact()
    except commands.BotMissingPermissions:
        # Don't DM the user if the bot can't send messages. We should
        # err on the side of caution and assume the bot was muted for a
        # good reason, and a DM wouldn't be a good idea in this case.
        if not ctx.me.permissions_in(ctx.channel).send_messages:
            # We shouldn't let this error propagate, since the bot wouldn't
            # be able to notify the user of missing perms anyways.
            return

        # Try sending it as DM, if it fails, raise the original
        # BotMissingPermissions exception
        #
        # Because we're raising the original exception we can't split this up
        # into functions without passing the actual error around.
        #
        # TODO: Make InteractiveSession.interact take an alternate destination
        #       argument so we don't have to copy ctx.
        new_ctx = copy.copy(ctx)
        # This is used for sending
        new_ctx.channel = paginator._channel = await ctx.author.create_dm()
        # paginator.interact checks if bot has permissions before running.
        paginator.context = new_ctx

        try:
            await paginator.interact()
        except discord.HTTPException:
            pass
        else:
            return

        # We can't DM the user. It's time to tell them that she can't send help.
        old_send = ctx.send

        async def new_send(content, **kwargs):
            content += ' You can also turn on DMs if you wish.'
            await old_send(content, **kwargs)

        ctx.send = new_send
        raise


def help_command(func=lambda s: s, **kwargs):
    """Create a help command with a given transformation function."""

    async def command(_, ctx, *, command: _HelpCommand = None):
        await _help(ctx, command, func=func)

    # command.module would be set to *here*. This is bad because the category
    # utilizes the module itself, and that means that the category would be
    # "utils" rather than what we really want. We could use some sort of proxy
    # descriptor to avoid doing framehacks but this is the simplest way.
    try:
        module = sys._getframe(1).f_globals.get('__name__', '__main__')
    except (AttributeError, ValueError):
        pass

    if module is not None:
        command.__module__ = module
    return commands.command(help=func("Shows this message and stuff"), **kwargs)(command)
