import asyncio
import contextlib
import discord
import inspect
import io
import itertools
import textwrap
import traceback

from discord.ext import commands

from .utils.context_managers import temp_attr

from core.cog import Cog


def _tabulate(rows, headers=()):
    display_rows = [list(map(str, r)) for r in rows]
    widths = [max(map(len, column)) for column in zip(*display_rows)]
    widths[:] = (max(len(c), w) for c, w in itertools.zip_longest(headers, widths, fillvalue=''))

    sep = '+'.join('-' * w for w in widths)
    sep = f'+{sep}+'

    to_draw = [sep]

    def get_entry(d):
        elem = '|'.join(f'{e:^{w}}' for e, w in zip(d, widths))
        return f'|{elem}|'

    if headers:
        to_draw.append(get_entry(headers))
        to_draw.append(sep)

    to_draw.extend(get_entry(row) for row in display_rows)
    to_draw.append(sep)
    return '\n'.join(to_draw)


class Owner(Cog):
    """Owner-only commands"""
    __hidden__ = True

    def __init__(self, bot):
        self.bot = bot
        self._last_result = None

    async def __local_check(self, ctx):
        return await ctx.bot.is_owner(ctx.author)

    def _create_env(self, ctx):
        return {
            'bot': self.bot,
            'ctx': ctx,
            'message': ctx.message,
            'guild': ctx.guild,
            'server': ctx.guild,
            'channel': ctx.channel,
            'author': ctx.author,
            **globals()
        }

    @commands.command()
    async def debug(self, ctx, *, code: str):
        """Evaluates code."""
        code = code.strip('` ')

        env = self._create_env(ctx)
        try:
            result = eval(code, env)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:
            await ctx.send(f'```py\n{traceback.format_exc()}```')
        else:
            await ctx.send(f'```py\n{result}```')

    @staticmethod
    def cleanup_code(body):
        # remove ```py\n```
        if body.startswith('```') and body.endswith('```'):
            return '\n'.join(body.split('\n')[1:-1])

        # remove `foo`
        return body.strip('` \n')

    @staticmethod
    def get_syntax_error(e):
        if e.text is None:
            return '```py\n{0.__class__.__name__}: {0}\n```'.format(e)
        return '```py\n{0.text}{1:>{0.offset}}\n{2}: {0}```'.format(e, '^', type(e).__name__)

    @commands.command(name='eval', aliases=['exec'])
    async def _eval(self, ctx, *, body: str):
        """Evaluates more code"""
        env = {**self._create_env(ctx), '_': self._last_result}
        body = self.cleanup_code(body)
        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'

        try:
            exec(to_compile, env)
        except SyntaxError as e:
            return await ctx.send(self.get_syntax_error(e))

        func = env['func']
        with io.StringIO() as stdout:
            try:
                with contextlib.redirect_stdout(stdout):
                    ret = await func()
            except Exception as e:
                value = stdout.getvalue()
                await ctx.send(f'```py\n{value}{traceback.format_exc()}\n```')
            else:
                value = stdout.getvalue()
                with contextlib.suppress(discord.HTTPException):
                    await ctx.message.add_reaction('\u2705')

                if ret is None:
                    if value:
                        await ctx.send(f'```py\n{value}\n```')
                else:
                    self._last_result = ret
                    await ctx.send(f'```py\n{value}{ret}\n```')

    @commands.command(hidden=True)
    async def sql(self, ctx, *, query: str):
        """Run some SQL."""
        # the imports are here because I imagine some people would want to use
        # this cog as a base for their other cog, and since this one is kinda
        # odd and unnecessary for most people, I will make it easy to remove
        # for those people.
        from .utils.formats import pluralize
        import time

        query = self.cleanup_code(query)

        is_multistatement = query.count(';') > 1
        async with ctx.db.get_session() as session:
            try:
                start = time.perf_counter()
                results = [dict(r) async for r in await session.cursor(query)]
                dt = (time.perf_counter() - start) * 1000.0
            except Exception:
                return await ctx.send(f'```py\n{traceback.format_exc()}\n```')

        if is_multistatement or not results:
            return await ctx.send(f'`{dt:.2f}ms: {results}`')

        print(results)
        num_rows = len(results)
        headers = list(results[0])
        rendered = _tabulate((list(r.values()) for r in results), headers)

        fmt = f'```\n{rendered}\n```\n*Returned {pluralize(row=num_rows)} in {dt:.2f}ms*'
        if len(fmt) > 2000:
            fp = io.BytesIO(fmt.encode('utf-8'))
            await ctx.send('Too many results...', file=discord.File(fp, 'results.txt'))
        else:
            await ctx.send(fmt)

    @commands.command()
    async def botav(self, ctx, *, avatar):
        with open(avatar, 'rb') as f:
            await self.bot.user.edit(avatar=f.read())
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    async def load(self, ctx, cog: str):
        """Loads a bot-extension (one with a setup method)"""
        ctx.bot.load_extension(cog)
        await ctx.send('Ok onii-chan~')

    @commands.command()
    async def unload(self, ctx, cog: str):
        """Unloads a bot-extension (one with a setup method)"""
        ctx.bot.unload_extension(cog)
        await ctx.send('Ok onii-chan~')

    @commands.command()
    async def reload(self, ctx, cog: str):
        """Reloads a bot-extension (one with a setup method)"""
        ctx.bot.unload_extension(cog)
        ctx.bot.load_extension(cog)
        await ctx.send('Ok onii-chan~')

    @load.error
    @unload.error
    @reload.error
    async def load_error(self, ctx, error):
        traceback.print_exc()
        await ctx.send("Baka! You didn't code me properly  >///<")

    @commands.command(aliases=['kys'])
    async def die(self, ctx):
        """Shuts the bot down"""
        await ctx.release()  # Needed because logout closes the DatabaseInterface.
        await ctx.send("Bye... Please don't forget about me.")
        await ctx.bot.logout()

    @commands.command(aliases=['restart'])
    async def reset(self, ctx):
        """Restarts the bot"""
        ctx.bot.reset_requested = True
        await ctx.send("Sleepy... zZzzzzZ...")
        await ctx.bot.logout()

    @commands.command()
    async def say(self, ctx, *, msg):
        await ctx.message.delete()
        # make sure commands for other bots (or even from itself) can't be executed
        await ctx.send(f"\u200b{msg}")

    @commands.command(name="sendmessage")
    async def send_message(self, ctx, channel: discord.TextChannel, *, msg):
        """Sends a message to a particular channel"""
        owner = (await self.bot.application_info()).owner
        await channel.send(f"Message from {owner}:\n{msg}")
        await ctx.send(f"Successfully sent message in {channel}: {msg}")

    @commands.command()
    async def do(self, ctx, num: int, *, command):
        """Repeats a command a given amount of times"""
        with temp_attr(ctx.message, 'content', command):
            for i in range(num):
                await self.bot.process_commands(ctx.message)

    @commands.command(aliases=['chaincmd'])
    async def chaincommand(self, ctx, *commands):
        for cmd in commands:
            with temp_attr(ctx.message, 'content', cmd):
                await self.bot.process_commands(ctx.message)
                # prevent rate-limiting.
                await asyncio.sleep(1)

    @commands.command()
    async def reacquire(self, ctx):
        """Test command for releasing and reacquiring the database session."""
        print('releasing session', ctx.session, 'current state:', ctx.session._state)
        s = ctx.session
        await ctx.send('Releasing...')
        await ctx.release()

        print('sleeping...', 'state of sessiom:', s._state)
        await asyncio.sleep(10)

        print('reacquiring...')
        await ctx.acquire()

        print('success!', ctx.session, 'state of new session:', ctx.session)
        await ctx.send('\N{OK HAND SIGN}')


def setup(bot):
    bot.add_cog(Owner(bot))
