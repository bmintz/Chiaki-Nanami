import copy
import discord
import json
import random

from discord.ext import commands
from datetime import datetime

from ..utils.converter import BotCogConverter, BotCommand
from ..utils.deprecated import deprecated
from ..utils.examples import wrap_example
from ..utils.formats import multi_replace
from ..utils.misc import emoji_url, truncate
from ..utils.paginator import CogPages, GeneralHelpPaginator, HelpCommandPage, ListPaginator

from core.cog import Cog


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


class TipPaginator(ListPaginator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.per_page = 1

    def _create_embed(self, idx, page):
        # page returns a tuple (because it returns a slice of entries)
        p = page[0]
        return (discord.Embed(colour=self.colour, description=p['description'])
                .set_author(name=f"#{idx + 1}: {p['title']}", icon_url=TIP_EMOJI)
                )


class HelpCommand(BotCommand):
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


async def _dm_send_fail(ctx, error):
    old_send = ctx.send

    async def new_send(content, **kwargs):
        content += ' You can also turn on DMs if you wish.'
        await old_send(content, **kwargs)

    ctx.send = new_send
    action = f'send {ctx.invoked_with}'
    await ctx.bot_missing_perms(error.missing_perms, action=action)


async def _maybe_dm_help(ctx, paginator, error):
    # We need to create a copy of the context object so that we can
    # keep an old copy if a logger or something wants to use it.
    new_ctx = copy.copy(ctx)
    new_ctx.channel = await ctx.author.create_dm()
    paginator.context = new_ctx

    try:
        await paginator.interact()
    except discord.HTTPException:
        # Avoid making another copy of the context if we don't need to.
        new_ctx.channel = ctx.channel
        await _dm_send_fail(new_ctx, error)


async def default_help(ctx, command=None, func=lambda s: s):
    if command is None:
        paginator = await GeneralHelpPaginator.create(ctx)
    else:
        paginator = HelpCommandPage(ctx, command, func)

    try:
        await paginator.interact()
    except commands.BotMissingPermissions as e:
        await _maybe_dm_help(ctx, paginator, e)


def default_help_command(func=lambda s: s, **kwargs):
    @commands.command(help=func("Shows this message and stuff"), **kwargs)
    async def help_command(self, ctx, *, command: HelpCommand=None):
        await default_help(ctx, command, func=func)
    return help_command



_bracket_repls = {
    '(': ')', ')': '(',
    '[': ']', ']': '[',
    '<': '>', '>': '<',
}


class Help(Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.remove_command('help')
        self.bot.remove_command('h')

        try:
            with open('data/tips.json') as f:
                self.tips_list = json.load(f)
        except FileNotFoundError:
            self.tips_list = []

    help = default_help_command(name='help', aliases=['h'])
    halp = default_help_command(str.upper, name='halp', aliases=['HALP'], hidden=True)
    pleh = default_help_command((lambda s: multi_replace(s[::-1], _bracket_repls)), name='pleh', hidden=True)
    pleh = default_help_command((lambda s: multi_replace(s[::-1].upper(), _bracket_repls)),
                                name='plah', aliases=['PLAH'], hidden=True)
    Halp = default_help_command(str.title, name='Halp', hidden=True)

    async def _invite_embed(self, ctx):        
        invite = (discord.Embed(description=self.bot.description, title=str(self.bot.user), colour=self.bot.colour)
                  .set_thumbnail(url=self.bot.user.avatar_url_as(format=None))
                  .add_field(name="Want me in your server?",
                             value=f'[Invite me here!]({self.bot.invite_url})', inline=False)
                  .add_field(name="If you just to be simple...",
                             value=f'[Invite me with minimal permissions!]({self.bot.minimal_invite_url})', inline=False)
                  .add_field(name="Need help with using me?",
                             value=f"[Here's the official server!]({self.bot.support_invite})", inline=False)
                  .add_field(name="If you're curious about how I work...",
                             value="[Check out the source code!](https://github.com/Ikusaba-san/Chiaki-Nanami/tree/rewrite)", inline=False)
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

    @deprecated(aliases=['cogs', 'mdls'], instead='help')
    async def modules(self, ctx):
        """Shows all the *visible* modules that I have loaded"""
        visible_cogs = (
            (name, cog.__doc__ or '\n')
            for name, cog in self.bot.cogs.items() if name and not cog.__hidden__
        )

        formatted_cogs = [
            f'`{name}` => {truncate(doc.splitlines()[0], 20, "...")}'
            for name, doc in visible_cogs
        ]

        modules_embed = (discord.Embed(title="List of my modules",
                                       description='\n'.join(formatted_cogs),
                                       colour=self.bot.colour)
                         .set_footer(text=f'Type `{ctx.prefix}help` for help.')
                         )
        await ctx.send(embed=modules_embed)

    @commands.command(name='commands', aliases=['cmds'])
    async def commands_(self, ctx, cog: BotCogConverter):
        """Shows all the *visible* commands I have in a given cog/module"""
        paginator = await CogPages.create(ctx, cog)
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
