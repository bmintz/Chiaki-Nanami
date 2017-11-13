import asyncio
import discord
import inspect
import itertools
import operator
import random
import textwrap
import time

from collections import Counter, OrderedDict
from discord.ext import commands
from more_itertools import sliced

from cogs.utils.context_managers import temp_attr
from cogs.utils.paginator import BaseReactionPaginator, ListPaginator, page


def _unique(iterable):
    return list(OrderedDict.fromkeys(iterable))


def _all_names(command):
    return [command.name, *command.aliases]


def _has_subcommands(command):
    return isinstance(command, commands.GroupMixin)


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
        footer = f'Module: {command.cog_name} | Click the info button below to see an example.'
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


_note = (
    "You can't commands that have\n"
    "been crossed out (~~`like this`~~)"
)


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
                .add_field(name='Commands', value='\n'.join(entries))
                .add_field(name='Note', value=_note, inline=False)
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

    @classmethod
    async def create(cls, ctx):
        def key(c):
            inst = c.instance
            cls = inst.__class__.name if inst else '\u200bMisc'
            return inst.__class__.__parent_category__, cls

        entries = (cmd for cmd in sorted(ctx.bot.commands, key=key) if not cmd.hidden)
        nested_pages = []
        per_page = 10

        # (cog, description, first 10 commands)
        # (cog, description, next 10 commands)
        # ...
        get_cog = ctx.bot.get_cog
        for (parent, name), cmds in itertools.groupby(entries, key=key):
            cog = get_cog(name)
            if getattr(cog, '__hidden__', False):
                continue

            if cog is None:
                description = 'This is all the misc commands!'
            else:
                description = inspect.getdoc(cog) or 'No description... yet.'

            lines = [' | '.join(line) async for line in _command_formatters(cmds, ctx)]
            nested_pages.extend((parent, name, description, page) for page in sliced(lines, per_page))

        self = cls(ctx, nested_pages, lines_per_page=1)  # needed to break the slicing in __getitem__
        return self

    def __len__(self):
        return self._num_extra_pages + super().__len__() + 1

    def __getitem__(self, idx):
        if 0 <= idx < self._num_extra_pages:
            self._index = idx
            return self._page_footer_embed(self._extra_pages[idx](self))
        elif idx in {-1, len(self) - 1}:
            if idx < 0:
                # normalize the index because -1 isn't technically allowed
                idx += len(self)

            self._index = idx
            return self.ending()

        result = super().__getitem__(idx - self._num_extra_pages)
        self._index = idx  # properly set the index
        return result

    def _page_footer_embed(self, embed, *, offset=0):
        return embed.set_footer(text=f'Currently on page {self._index + offset + 1}/{len(self)}')

    def _create_embed(self, idx, page):
        _, name, description, lines = page[0]
        note = f'Type `{self.context.clean_prefix}help command`\nfor more info on a command.'
        commands = '\n'.join(lines) + f'\n\n{note}'

        return self._page_footer_embed(
            discord.Embed(colour=self.colour, description=description)
            .set_author(name=name)
            .add_field(name='Commands', value=commands)
            .add_field(name='Note', value=_note, inline=False),
            offset=self._num_extra_pages
        )

    @page('\N{NOTEBOOK WITH DECORATIVE COVER}')
    def jump_to_commands(self):
        """Jump to the table of contents"""
        return self[1]

    def intro(self):
        """The intro, ie the thing you just saw."""
        instructions = (
            'Press \N{BLACK RIGHT-POINTING TRIANGLE} to see what this help has in store!\n'
            'For extra help, go to the last page (press \N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR})\n'
            'for some useful links and commands.'
        )

        return (discord.Embed(colour=self.colour, description=self.context.bot.description)
                .set_author(name=f"Hi, {self.context.author}. I'm {self.context.bot.user}!")
                .add_field(name="Welcome to my help page!", value=instructions, inline=False)
                .set_image(url=CHIAKI_INTRO_URL)
                )

    def instructions(self):
        """How to navigate through this help page"""
        description = (
            'This is a paginator run on reactions. To navigate\n'
            'through this help page, you must click on any of\n'
            'the reactions below.\n'
        )

        return (discord.Embed(colour=self.colour, description=description)
                .set_author(name='How to use the help page')
                .add_field(name='Here are all of the reactions', value=self.reaction_help, inline=False)
                )

    def table_of_contents(self):
        """Table of contents (this page)"""
        extra_docs = enumerate(map(inspect.getdoc, self._extra_pages), start=1)
        extra_lines = itertools.starmap('`{0}` - {1}'.format, extra_docs)
        counter = self._num_extra_pages + 1

        def cog_pages(iterator):
            nonlocal counter
            name_counter = Counter(map(operator.itemgetter(1), iterator))
            for name, count in name_counter.items():
                if count == 1:
                    yield str(counter), name
                else:
                    yield f'{counter}-{counter + count - 1}', name
                counter += count

        embed = (discord.Embed(colour=self.colour, description='\n'.join(extra_lines))
                 .set_author(name='Table of Contents')
                 )

        for category, entries in itertools.groupby(self.entries, operator.itemgetter(0)):
            pairs = list(cog_pages(entries))
            padding = max(len(p[0]) for p in pairs)
            lines = (f'`\u200b{numbers:<{padding}}\u200b` - {name}' for numbers, name in pairs)

            embed.add_field(name=category.title(), value='\n'.join(lines), inline=False)

        return embed.add_field(name='Other', value=f'`{len(self)}` - Some useful links.', inline=False)

    def how_to_use(self):
        """How to use the bot"""
        description = (
            'The signature is actually pretty simple!\n'
            "It's always there in the \"Signature\" field when\n"
            f'you do `{self.context.clean_prefix} help command`.'
        )

        note = textwrap.dedent('''
            **Do not type in the brackets!**
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

    def ending(self):
        """End of the help page, and info about the bot."""
        bot = self.context.bot
        support = f'Go to the support server here!\n{bot.support_invite}'
        useful_links = (
            f'[Click me to invite me to your server!]({bot.invite_url})\n'
            "[Check the code out here (it's fire!)](https://github.com/Ikusaba-san/Chiaki-Nanami)\n"
        )

        feedback = f'Use `{self.context.prefix}feedback your message`!'

        return (discord.Embed(colour=self.colour)
                .set_thumbnail(url=bot.user.avatar_url)
                .set_author(name="You've reached the end of the help page!")
                .add_field(name='For more help', value=support, inline=False)
                .add_field(name='To suggest any new features or bug fixes', value=feedback, inline=False)
                .add_field(name='And for some other useful links...', value=useful_links, inline=False)
                )

    @page('\N{BLACK SQUARE FOR STOP}')
    async def stop(self):
        """Exit the help page"""
        super().stop()

        # Only do it for a minute, so if someone does a quick stop,
        # we'll grant them their wish of stopping early.
        end = time.monotonic()
        if end - self._start_time < 60:
            return

        final_embed = (discord.Embed(colour=self.colour, description='*Just remember...* \N{HEAVY BLACK HEART}')
                       .set_author(name='Thank you for looking at the help page!')
                       .set_image(url=CHIAKI_MOTIVATION_URL)
                       )

        # haaaaaaaaaaaack
        await self._message.edit(embed=final_embed)
        return await asyncio.sleep(10)

    _extra_pages = [
        intro,
        table_of_contents,
        instructions,
        # how_to_use,
    ]
    _num_extra_pages = len(_extra_pages)

    @page('\N{WHITE QUESTION MARK ORNAMENT}')
    def signature(self):
        """Shows how to use the bot."""
        return self.how_to_use()

    async def interact(self, **kwargs):
        self._start_time = time.monotonic()
        await super().interact(**kwargs)


rmap = GeneralHelpPaginator._reaction_map
# signature is currently at the beginning so we need to move it to the end
rmap.move_to_end('\N{WHITE QUESTION MARK ORNAMENT}')
del rmap


class ChiakiFormatter(commands.HelpFormatter):
    def get_ending_note(self):
        return f"Type {self.clean_prefix}help command for more info on a command."

    async def format_help_for(self, ctx, command, func=lambda s: s):
        self.apply_function = func
        return await super().format_help_for(ctx, command)

    async def format(self):
        if self.is_bot():
            return await GeneralHelpPaginator.create(self.context)
        elif self.is_cog():
            return await CogPages.create(self.context, self.command)
        return HelpCommandPage(self.context, self.command, self.apply_function)
