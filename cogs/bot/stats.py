import discord
import psutil
from discord.ext import commands

class Stats:
    def __init__(self, bot):
        self.bot = bot
        self.process = psutil.Process()

    @commands.command(name='stats')
    @commands.bot_has_permissions(embed_links=True)
    async def stats(self, ctx):
        """Shows some general statistics about the bot.

        Do not confuse this with `{prefix}about` which is just the
        general info. This is just numbers.
        """

        bot = self.bot

        with self.process.oneshot():
            memory_usage_in_mb = self.process.memory_full_info().uss / 1024**2
            cpu_usage = self.process.cpu_percent() / psutil.cpu_count()

        uptime_seconds = bot.uptime.total_seconds()

        presence = (
            f'{bot.guild_count} Servers\n'
            f'{ilen(bot.get_all_channels())} Channels\n'
            f'{bot.user_count} Users'
        )

        chiaki_embed = (discord.Embed(description=bot.appinfo.description, colour=self.bot.colour)
                        .set_author(name=str(ctx.bot.user), icon_url=bot.user.avatar_url)
                        .add_field(name='CPU Usage', value=f'{cpu_usage}%\n{memory_usage_in_mb :.2f}MB')
                        .add_field(name='Presence', value=presence)
                        .add_field(name='Uptime', value=self.bot.str_uptime.replace(', ', '\n'))
                        )
        await ctx.send(embed=chiaki_embed)

def setup(bot):
    bot.add_cog(Stats(bot))
