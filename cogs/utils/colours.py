import aiohttp
import asyncio
import discord
import functools

from colorthief import ColorThief
from io import BytesIO

from . import cache


@cache.cache(maxsize=4096)
async def _read_image_from_url(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()


@cache.cache(maxsize=4096)
async def _dominant_color_from_url(url):
    """Returns an rgb tuple consisting the dominant color given a image url."""
    with BytesIO(await _read_image_from_url(url)) as f:
        # TODO: Make my own color-grabber module. This is ugly as hell.
        loop = asyncio.get_event_loop()
        get_colour = functools.partial(ColorThief(f).get_color, quality=1)
        return await loop.run_in_executor(None, get_colour)


async def url_color(url):
    return discord.Colour.from_rgb(*(await _dominant_color_from_url(url)))
url_colour = url_color


async def user_color(user):
    return await url_color(user.avatar_url_as(static_format='png'))
user_colour = user_color
