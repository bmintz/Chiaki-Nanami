import json
import random
from datetime import datetime

import discord
from discord.ext import commands

from ..utils.converter import Category
from ..utils.examples import wrap_example
from ..utils.formats import multi_replace
from ..utils.help import CogPages, help_command
from ..utils.misc import emoji_url
from ..utils.paginator import Paginator

CHIAKI_TIP_EPOCH = datetime(2017, 8, 24)
TIP_EMOJI = emoji_url('\N{ELECTRIC LIGHT BULB}')
DEFAULT_TIP = {
    'title': 'You have reached the end of the tips!',
    'description': 'Wait until the next update for more tips!'
}
TOO_FAR_TIP = {
    'title': "You're going a bit too far here!",
    'description': 'Wait until tomorrow or something!'
}


def _get_tip_index():
    return (datetime.utcnow() - CHIAKI_TIP_EPOCH).days


def positive_index(s):
    num = int(s)
    if num <= 0:
        raise commands.BadArgument('Value must be positive.')
    return num

@wrap_example(positive_index)
def _positive_index_example(ctx):
    return random.randint(1, len(ctx.bot.get_cog('Help').tips_list))


class TipPaginator(Paginator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, per_page=1, **kwargs)

    def create_embed(self, page):
        # page returns a tuple (because it returns a slice of entries)
        p = page[0]
        return (discord.Embed(colour=self.colour, description=p['description'])
                .set_author(name=f"#{self._index + 1}: {p['title']}", icon_url=TIP_EMOJI)
                )


_bracket_repls = {
    '(': ')', ')': '(',
    '[': ']', ']': '[',
    '<': '>', '>': '<',
}


class Help:
    def __init__(self, bot):
        self.bot = bot
        self.bot.remove_command('help')
        self.bot.remove_command('h')

        try:
            with open('data/tips.json') as f:
                self.tips_list = json.load(f)
        except FileNotFoundError:
            self.tips_list = []

    help = help_command(name='help', aliases=['h'])
    halp = help_command(str.upper, name='halp', hidden=True)
    pleh = help_command((lambda s: multi_replace(s[::-1], _bracket_repls)), name='pleh', hidden=True)
    pleh = help_command((lambda s: multi_replace(s[::-1].upper(), _bracket_repls)),
                        name='plah', hidden=True)

    async def _invite_embed(self, ctx):
        # TODO: Move this somewhere else as this is also duplicated in meta.py
        source_url = f'https://github.com/Ikusaba-san/Chiaki-Nanami'
        if ctx.bot.version_info.releaselevel != 'alpha':
            source_url = f'{source_url}/tree/v{ctx.bot.__version__}'

        invite = (discord.Embed(description=self.bot.description, title=str(self.bot.user), colour=self.bot.colour)
                  .set_thumbnail(url=self.bot.user.avatar_url_as(format=None))
                  .add_field(name="Want me in your server?",
                             value=f'[Invite me here!]({self.bot.invite_url})', inline=False)
                  .add_field(name="If you just to be simple...",
                             value=f'[Invite me with minimal permissions!]({self.bot.minimal_invite_url})', inline=False)
                  .add_field(name="Need help with using me?",
                             value=f"[Here's the official server!]({self.bot.support_invite})", inline=False)
                  .add_field(name="If you're curious about how I work...",
                             value=f"[Check out the source code!]({source_url})", inline=False)
                  )
        await ctx.send(embed=invite)

    @commands.command()
    async def invite(self, ctx):
        """...it's an invite"""
        if ctx.bot_has_embed_links():
            await self._invite_embed(ctx)
        else:
            content = (
                'Okay~ Here you go... I think. ^.^'
                f'Full Permissions: <{self.bot.invite_url}>'
                f'Minimal Permissions: <{self.bot.minimal_invite_url}>'
            )
            await ctx.send(content)

    @commands.command(name='commands', aliases=['cmds'])
    async def commands_(self, ctx, category: Category = None):
        """Shows all the commands in a given category.

        If no category is given, all commands are shown.
        """
        if category is None:
            return await ctx.invoke(self.help)
        paginator = await CogPages.create(ctx, category)
        await paginator.interact()

    async def _show_tip(self, ctx, number):
        if number > _get_tip_index() + 1:
            tip, success = TOO_FAR_TIP, False
        else:
            try:
                tip, success = self.tips_list[number - 1], True
            except IndexError:
                tip, success = DEFAULT_TIP, False

        tip_embed = discord.Embed.from_data(tip)
        tip_embed.colour = ctx.bot.colour
        if success:
            tip_embed.set_author(name=f'Tip of the Day #{number}', icon_url=TIP_EMOJI)

        await ctx.send(embed=tip_embed)

    @commands.command()
    @commands.bot_has_permissions(embed_links=True)
    async def tip(self, ctx, number: positive_index = None):
        """Shows a Chiaki Tip via number.

        If no number is specified, it shows the daily tip.
        """
        if number is None:
            number = _get_tip_index() + 1

        await self._show_tip(ctx, number)

    @commands.command()
    async def tips(self, ctx):
        """Shows all tips *up to today*"""
        current_index = _get_tip_index() + 1
        await TipPaginator(ctx, self.tips_list[:current_index]).interact()

    @commands.command()
    @commands.bot_has_permissions(embed_links=True)
    async def randomtip(self, ctx):
        """Shows a random tip.

        The tip range is from the first one to today's one.
        """
        number = _get_tip_index() + 1
        await self._show_tip(ctx, random.randint(1, number))

    @commands.command()
    @commands.cooldown(rate=1, per=60, type=commands.BucketType.user)
    async def feedback(self, ctx, *, message):
        """Gives feedback about the bot.

        This is a quick and easy way to either request features
        or bug fixes without being in the support server.

        You can only send feedback once every minute.
        """

        dest = self.bot.feedback_destination
        if not dest:
            return

        # Create the feedback embed
        embed = (discord.Embed(colour=ctx.bot.colour, description=message, title='Feedback')
                 .set_author(name=str(ctx.author), icon_url=ctx.author.avatar_url)
                 .set_footer(text=f'Author ID: {ctx.author.id}')
                 )

        if ctx.guild:
            embed.add_field(name='From', value=f'#{ctx.channel}\n(ID: {ctx.channel.id})', inline=False)
            embed.add_field(name='In', value=f'{ctx.guild}\n(ID: {ctx.guild.id})', inline=False)
        else:
            embed.add_field(name='From', value=f'{ctx.channel}', inline=False)

        embed.timestamp = ctx.message.created_at

        await dest.send(embed=embed)
        await ctx.send(':ok_hand:')


def setup(bot):
    bot.add_cog(Help(bot))
