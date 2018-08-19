import inspect
import os
import platform
import re

import discord
from discord.ext import commands

from ..utils.converter import BotCommand
from ..utils.formats import truncate
from ..utils.paginator import Paginator, paginated
from ..utils.subprocesses import run_subprocess

# --------- Changelog functions -----------

# Some useful regexes
VERSION_HEADER_PATTERN = re.compile(r'^## (\d+\.\d+\.\d+) - (\d{4}-\d{2}-\d{2}|Unreleased)$')
CHANGE_TYPE_PATTERN = re.compile(r'^### (Added|Changed|Deprecated|Removed|Fixed|Security)$')

def _is_bulleted(line):
    return line.startswith(('* ', '- '))

def _changelog_versions(lines):
    version = change_type = release_date = None
    changes = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue

        match = VERSION_HEADER_PATTERN.match(line)
        if match:
            if version:
                yield version, {'release_date': release_date, 'changes': changes.copy()}
            version = match[1]
            release_date = match[2]
            changes.clear()
            continue

        match = CHANGE_TYPE_PATTERN.match(line)
        if match:
            change_type = match[1]
            continue

        if _is_bulleted(line):
            changes.setdefault(change_type, []).append(line)
        else:
            changes[change_type][-1] += ' ' + line.lstrip()
    yield version, {'release_date': release_date, 'changes': changes.copy()}

def _load_changelog():
    with open('CHANGELOG.md') as f:
        return dict(_changelog_versions(f))

_CHANGELOG = _load_changelog()

def _format_line(line):
    if _is_bulleted(line):
        return '\u2022 ' + line[2:]
    return line

def _format_changelog_without_embed(version):
    changes = _CHANGELOG[version]
    nl_join = '\n'.join
    change_lines = '\n\n'.join(
        f'**{type_}**\n{nl_join(map(_format_line, lines))}'
        for type_, lines in changes['changes'].items()
    )
    return f'**__Version {version} \u2014 {changes["release_date"]}__**\n\n{change_lines}'

def _format_changelog_with_embed(version):
    changes = _CHANGELOG[version]
    nl_join = '\n'.join
    change_lines = '\n\n'.join(
        f'**__{type_}__**\n{nl_join(map(_format_line, lines))}'
        for type_, lines in changes['changes'].items()
    )
    embed = discord.Embed(description=change_lines)

    if changes['release_date'] == 'Unreleased':
        url = discord.Embed.Empty
    else:
        url = f'https://github.com/Ikusaba-san/Chiaki-Nanami/releases/tag/v{version}'

    name = f'Version {version} \u2014 {changes["release_date"]}'
    embed.set_author(name=name, url=url)
    return embed

# ----------------------------------------

class Meta:
    """Need some info about the bot? Here you go!"""
    def __init__(self, bot):
        self.bot = bot

        if bot.version_info.releaselevel == 'alpha':
            branch = 'master'
        else:
            branch = 'v' + bot.__version__

        self._source_url = f'https://github.com/Ikusaba-san/Chiaki-Nanami/tree/{branch}'

    @property
    def source_url(self):
        source_url = self._source_url
        return source_url.rsplit('/', 2)[0] if source_url.endswith('/tree/master') else source_url

    @commands.command()
    @commands.bot_has_permissions(embed_links=True)
    async def about(self, ctx):
        """Shows some info about the bot."""
        bot = ctx.bot

        links = (
            f'**[Add me]({bot.invite_url})** | '
            f'**[Support]({bot.support_invite})** | '
            f'**[Github]({self.source_url})** | '
            f'**[Upvote](https://discordbots.org/bot/247863665598922762/vote)**'
        )

        field_value = (
            f'Created by **{bot.creator}**\n'
            f'Version: **{bot.__version__}**\n'
            f'Watching **{bot.guild_count}** servers\n'
            f'Playing with **{bot.user_count}** people\n'
            f'Running **Python {platform.python_version()}**\n'
        )

        embed = (discord.Embed(colour=bot.colour, description=f'{links}\n{bot.description}')
                 .set_thumbnail(url=bot.user.avatar_url)
                 .set_author(name=f'About {bot.user.name}')
                 .add_field(name='\u200b', value=field_value, inline=False)
                 )
        await ctx.send(embed=embed)

    # ----------------- Github Related Commands -------------------

    # Credits to Reina
    @staticmethod
    async def _get_github_url():
        url, _ = await run_subprocess('git remote get-url origin')
        return url.strip()[:-4]  # remove .git\n

    async def _get_recent_commits(self, *, limit=None):
        url = await self._get_github_url()
        cmd = f'git log --pretty=format:"[`%h`]({url}/commit/%H) <%s> (%cr)"'
        if limit is not None:
            cmd += f' -{limit}'

        return (await run_subprocess(cmd))[0]

    async def _display_raw(self, ctx, lines):
        paginator = commands.Paginator(prefix='```py')
        for line in lines:
            # inspect.getsourcelines returns the lines with the newlines at the
            # end. However, the paginator will add it's own newlines when joining
            # up the lines. We don't want to have double lines. So we have to
            # strip off the ends.
            #
            # Also, because we prefix each page with a code block (```), we need
            # to make sure that other triple-backticks don't prematurely end the
            # block.
            paginator.add_line(line.rstrip().replace('`', '\u200b`'))

        for p in paginator.pages:
            await ctx.send(p)

    @commands.command()
    async def source(self, ctx, *, command: BotCommand = None):
        """Displays the source code for a command.

        If the source code has too many lines \u2014 10 lines for me \u2014
        it displays the Github URL.
        """
        source_url = self._source_url
        if command is None:
            if source_url.endswith('/tree/master'):
                source_url = source_url.rsplit('/', 2)[0]  # For cleanness
            return await ctx.send(source_url)

        src = command.callback.__code__
        lines, firstlineno = inspect.getsourcelines(command.callback)
        if len(lines) < 10:
            return await self._display_raw(ctx, lines)

        lastline = firstlineno + len(lines) - 1
        # We don't use the built-in commands so we can eliminate this branch
        location = os.path.relpath(src.co_filename).replace('\\', '/')

        url = f'<{source_url}/{location}#L{firstlineno}-L{lastline}>'
        await ctx.send(url)

    @commands.command()
    @paginated()
    async def commits(self, ctx, limit=10):
        """Shows the latest changes made to the bot.

        The default is the latest 10 changes.
        """
        changes = await self._get_recent_commits(limit=limit)

        def truncate_sub(m):
            return truncate(m[1], 47, "...")

        # By default git show doesn't truncate the commit messages.
        # %<(N,trunc) truncates them but it also pads messages that are
        # shorter than N columns, which is NOT what we want.
        #
        # One attempt was to use sed as shown here:
        # https://stackoverflow.com/a/24604658
        #
        # However since we're attempting to make this a cross-platform bot,
        # we can't use sed as it's not available in Windows and there's no
        # equivalent of it, causing it to fail. As a result, we're forced to
        # use regex.
        #
        # We know two things for sure about the commit line:
        # 1. The hash hyper link goes by the format of [`{hash}`]({commit_url})
        # 2. The relative committer time is wrapped in parentheses, i.e. ({delta})
        #
        # We use a regex solution to fish out the commit message, which
        # is wrapped in <> from the function above since we know for sure
        # neither the hash or commiter date will have <> in them.
        #
        # Not sure what the performance backlash is since it's regex,
        # but from naive timings it doesn't look like it takes too long.
        # (only 3 ms, which isn't that much compared to HTTP requests.)

        lines = (
            re.sub(r'<(.*)>', truncate_sub, change)
            for change in changes.splitlines()
        )

        pages = Paginator(ctx, lines, title='Latest Changes', per_page=10)
        await pages.interact()

    @commands.command()
    async def changelog(self, ctx):
        """Shows the latest important changes"""
        version = '.'.join(map(str, ctx.bot.version_info[:3]))
        if not ctx.bot_has_embed_links():
            return await ctx.send(_format_changelog_without_embed(version))

        embed = _format_changelog_with_embed(version)
        embed.colour = ctx.bot.colour
        author = embed.author
        bot_icon = ctx.bot.user.avatar_url_as(static_format='png')
        embed.set_author(name=author.name, url=author.url, icon_url=bot_icon)

        await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(Meta(bot))
