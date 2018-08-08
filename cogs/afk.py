import contextlib
import enum
from collections import defaultdict, deque
from datetime import datetime

import discord
from discord.ext import commands

from .utils import time
from .utils.colours import user_color
from .utils.jsonf import JSONFile


class AFKConfig(enum.IntEnum):
    MAX_MESSAGES = 5
    MAX_INTERVAL = 10 * 60


class AFK:
    def __init__(self, bot):
        self.bot = bot
        # Debating whether or not I should use a DB. Because this would be queried
        # for EVERY message, making it extremely intense.
        self.afks = JSONFile("afk.json")
        self.afk_configs = JSONFile('afkconfig.json')
        self.user_message_queues = defaultdict(deque)

    async def _get_afk_embed(self, member):
        message = self.afks.get(member.id)
        if message is None:
            return None

        avatar = member.avatar_url
        colour = await user_color(member)
        title = f"{member.display_name} is AFK"

        embed = (discord.Embed(description=message, colour=colour)
                 .set_author(name=title, icon_url=avatar)
                 .set_footer(text=f"ID: {member.id}")
                 )

        with contextlib.suppress(IndexError):
            embed.timestamp = self.user_message_queues[member.id][-1]
        return embed

    def _has_messaged_too_much(self, author):
        message_queue = self.user_message_queues[author.id]
        if len(message_queue) <= AFKConfig.MAX_MESSAGES:
            return False

        delta = (message_queue.popleft() - datetime.now()).total_seconds()
        return delta >= AFKConfig.MAX_INTERVAL

    async def _remove_afk(self, author):
        await self.afks.remove(author.id)
        self.user_message_queues.pop(author.id, None)

    def _afk_messages_enabled(self, server):
        if server.id not in self.afk_configs:
            return False

        return self.afk_configs[server.id]['send_afk_message']

    @commands.command()
    async def afk(self, ctx, *, message: str = None):
        """Sets your AFK message"""
        member = ctx.author
        if message is None:
            if member.id not in self.afks:
                return await ctx.send("You need a message... I think.")

            await self._remove_afk(member)
            await ctx.send("You are no longer AFK")
        else:
            await self.afks.put(member.id, message)
            await ctx.send("You are AFK")

    @commands.command(name='afksay')
    @commands.has_permissions(manage_guild=True)
    async def afk_say(self, ctx, send_afk_message: bool):
        """Sets whether or not I should say the user's AFK message when mentioned.

        This is useful in places where the AFK message might be extremely spammy.
        This is server-wide at the moment
        """
        config = self.afk_configs.get(ctx.guild.id, {'send_afk_message': False})
        config['send_afk_message'] = send_afk_message
        await self.afk_configs.put(ctx.guild.id, config)
        await ctx.send('\N{THUMBS UP SIGN}')

    async def check_user_message(self, message):
        author, guild = message.author, message.guild
        if not self._afk_messages_enabled(guild):
            return

        if author.id == self.bot.user.id:
            return

        if author.id not in self.afks:
            return

        self.user_message_queues[author.id].append(message.created_at)
        if self._has_messaged_too_much(author):
            await self._remove_afk(author)
            await message.channel.send(
                f"{author.mention}, you are no longer AFK as you have messaged "
                f"{AFKConfig.MAX_MESSAGES} times in less than "
                f"{time.duration_units(AFKConfig.MAX_INTERVAL)}."
            )

    async def check_user_mention(self, message):
        if not self._afk_messages_enabled(message.guild):
            return

        if message.author.id == self.bot.user.id:
            return

        for user in message.mentions:
            afk_embed = await self._get_afk_embed(user)
            if afk_embed:
                await message.channel.send(embed=afk_embed)

    async def on_message(self, message):
        if message.guild is None:
            return

        await self.check_user_message(message)
        await self.check_user_mention(message)

def setup(bot):
    bot.add_cog(AFK(bot))
