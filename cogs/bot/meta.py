import discord
import inspect
import os
import platform
import re

from discord.ext import commands

from ..utils.converter import BotCommand
from ..utils.formats import truncate
from ..utils.subprocesses import run_subprocess
from ..utils.paginator import Paginator, paginated


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
            f'**[Github]({self.source_url})**'
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


def setup(bot):
    bot.add_cog(Meta(bot))
