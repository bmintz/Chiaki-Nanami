import inspect
import os
import platform
import re

import discord
from discord.ext import commands

from ..utils.converter import BotCommand

# --------- Changelog functions -----------

# Some useful regexes
# NOTE: As cool as it would be to link the commits to the corresponding commits
#       on GitHub, in practice it would quickly bloat the changelog and would
#       increase the risk of causing 400s on long changelogs.
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

        self._source_url = f'https://github.com/bmintz/Chiaki-Nanami/tree/{branch}'

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
