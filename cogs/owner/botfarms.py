import discord
import math

from discord.ext import commands

from ..utils import disambiguate


# These functions are usually used for doing ratings
# but here I'm using them to calculate if a server *might*
# be a "bot farm". More explanation in the function itself.

def _pnormaldist(n):
    b = [1.570796288, 0.03706987906, -0.8364353589e-3,
         -0.2250947176e-3, 0.6841218299e-5, 0.5824238515e-5,
         -0.104527497e-5, 0.8360937017e-7, -0.3231081277e-8,
         0.3657763036e-10, 0.6936233982e-12]

    if not 0 <= n <= 1:
        return 0

    if n == 0.5:
        return 0

    w1 = n
    if n > 0.5:
        w1 = 1 - w1

    w3 = -math.log(4.0 * w1 * (1.0 - w1))
    iter_b = iter(b)
    w1 = next(iter_b) + sum(v * w3 ** i for i, v in enumerate(iter_b, 1))

    result = math.sqrt(w1 * w3)
    return result if n > 0.5 else -result


def _ci_lower_bound(pos, n, confidence):
    if pos > n:
        raise ValueError('number of positive ratings must be lower than the total')

    if n == 0:
        return 0

    z = _pnormaldist(1-(1-confidence)/2)
    phat = 1.0*pos/n
    return (phat + z*z/(2*n) - z * math.sqrt((phat*(1-phat)+z*z/(4*n))/n))/(1+z*z/n)


class AntiBotCollections:
    """Commands related to "bot-collection" servers -- servers that have a
    high ratio of bots to humans.
    """
    # Determining if a server is a "bot collection server" is no easy task,
    # because there are a lot of edge cases in servers where it might not be
    # a bot farm but merely a testing server with only a few bots.
    @staticmethod
    def is_bot_farm(guild):
        """Return True if the guilds is considered to be a "bot farm".

        Bot farms are guilds where the bot-to-member ratio is extremely high.
        They're essentially bot collection servers. These are very useless
        to the bot and could even pose problems as people in those bot farms
        like to hammer bots with a lot of commands.
        """
        bots = sum(m.bot for m in guild.members)
        total = guild.member_count

        return _ci_lower_bound(bots, total, 0.9) >= 0.42

    @commands.command(name='leavebotfarms')
    @commands.is_owner()
    async def leave_bot_farms(self, ctx):
        """Leaves any servers that are considered to be "bot collections".

        A bot collection server is a server that has a high
        ratio of bots to members.
        """
        bot_farms = list(filter(self.is_bot_farm, ctx.bot.guildsview()))
        if not bot_farms:
            return await ctx.send("Thankfully I'm not in any bot collections...")

        for g in bot_farms:
            await g.leave()

        await ctx.send(f"Left **{len(bot_farms)}** servers. Hope you don't miss them, cuz I don't!")

    @commands.command(name='isbotfarm')
    @commands.is_owner()
    async def _is_bot_farm(self, ctx, *, server: disambiguate.Guild):
        """Checks if a server is considered to be a "bot collection" server.

        A bot collection server is a server that has a high
        ratio of bots to members.
        """
        bots = sum(m.bot for m in server.members)
        total = server.member_count

        bot_farm = self.is_bot_farm(server)
        description = (
            f'**Members**: {total}\n'
            f'**Bots**: {bots}\n'
        )

        if bot_farm:
            message = f'**Yes**, {server} is a \nbot collection server'
            colour = 0xF44336
        else:
            colour = 0x4CAF50
            message = f'**No.** {server} is not \na bot collection server'

        embed = (discord.Embed(colour=colour, description=description)
                 .set_author(name=server.name)
                 .add_field(name='Is it a bot collection server?', value=message)
                 )

        if server.icon:
            embed.set_thumbnail(url=server.icon_url)

        await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(AntiBotCollections())
