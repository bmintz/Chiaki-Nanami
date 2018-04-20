import discord
import random
import string

from discord.ext import commands
from itertools import starmap

from core.cog import Cog


_prefixes = list(set(string.punctuation) - {'@', '#'})


class Prefix(commands.Converter):
    async def convert(self, ctx, argument):
        if not argument:
            raise commands.BadArgument("I need an actual prefix...")

        if not argument.strip():
            raise commands.BadArgument("A space isn't a prefix, you know...")

        user_id = ctx.bot.user.id
        if argument.startswith((f'<@{user_id}>', f'<@!{user_id}>')):
            raise commands.BadArgument('That is a reserved prefix already in use.')
        return argument

    @staticmethod
    def random_example(ctx):
        return random.choice(_prefixes)


class RemovablePrefix(Prefix):
    @staticmethod
    def random_example(ctx):
        return random.choice(ctx.bot.get_raw_guild_prefixes(ctx.guild))


class Prefixes(Cog):
    @commands.group(aliases=['prefixes'], invoke_without_command=True)
    async def prefix(self, ctx):
        """Shows the prefixes that you can use in this server."""
        if ctx.invoked_subcommand is not None:
            return

        prefixes = ctx.bot.get_guild_prefixes(ctx.guild)
        # remove the duplicate mention prefix, so the mentions don't show up twice
        del prefixes[-1]

        description = '\n'.join(starmap('`{0}.` {1}'.format, enumerate(prefixes, start=1)))
        embed = discord.Embed(title=f'Prefixes you can use in {ctx.guild}',
                              colour=ctx.bot.colour, description=description)
        await ctx.send(embed=embed)

    @prefix.command(name='add', ignore_extra=False)
    @commands.has_permissions(manage_guild=True)
    async def add_prefix(self, ctx, prefix: Prefix):
        """Adds a custom prefix for this server.

        To have a word prefix, you should quote it and end it with a space, e.g.
        "hello " to set the prefix to "hello ". This is because Discord removes
        spaces when sending messages so the spaces are not preserved.

        (Unless, you want to do hellohelp or something...)

        Multi-word prefixes must be quoted also.
        """
        prefixes = ctx.bot.get_raw_guild_prefixes(ctx.guild)
        if prefix in prefixes:
            return await ctx.send(f"\"{prefix}\" was already a custom prefix...")

        prefixes += (prefix, )
        await ctx.bot.set_guild_prefixes(ctx.guild, prefixes)
        await ctx.send(f"Successfully added prefix \"{prefix}\"!")

    @prefix.command(name='remove', ignore_extra=False)
    @commands.has_permissions(manage_guild=True)
    async def remove_prefix(self, ctx, prefix: RemovablePrefix):
        """Removes a prefix for this server.

        This is effectively the inverse to `{prefix}prefix add`.
        """
        prefixes = list(ctx.bot.get_raw_guild_prefixes(ctx.guild))
        if not prefixes:
            return await("This server doesn't use any custom prefixes")

        try:
            prefixes.remove(prefix)
        except ValueError:
            return await ctx.send(f'"{prefix}" isn\'t mine...')

        await ctx.bot.set_guild_prefixes(ctx.guild, prefixes)
        await ctx.send(f"Successfully removed \"{prefix}\"!")

    @add_prefix.error
    @remove_prefix.error
    async def prefix_error(self, ctx, error):
        if isinstance(error, commands.TooManyArguments):
            await ctx.send("Nya~~! Too many! Go slower or put it in quotes!")
        else:
            original = getattr(error, 'original', None)
            if original:
                await ctx.send(original)

    @prefix.command(name='reset', aliases=['clear'])
    @commands.has_permissions(manage_guild=True)
    async def reset_prefix(self, ctx):
        """Removes all the server's prefixes.

        I will only respond to mentions after this.
        """

        await ctx.bot.set_guild_prefixes(ctx.guild, [])
        await ctx.send(f"Done. Please mention me if you need anything.")


def setup(bot):
    bot.add_cog(Prefixes(bot))
