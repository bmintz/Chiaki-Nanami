"""Module used for creating the "urls.json" file"""

import discord
import pathlib
import json

from more_itertools import chunked

__all__ = ['upload_images', 'save_image_urls', 'get_card_image_file', 'get_card_image_url']

_CARD_IMG_PATH = pathlib.Path('data/images/cards')
_CARD_IMG_URLS_PATH = _CARD_IMG_PATH / 'urls.json'
_card_image_urls = {}

try:
    with open(_CARD_IMG_URLS_PATH) as f:
        urls = json.load(f)
except Exception:
    import logging
    logging.exception(f'Error occured when loading {_CARD_IMG_URLS_PATH}')
else:
    _card_image_urls.update(urls)


async def upload_images(channel):
    """Upload the images to Discord and return the resulting attachments"""

    result = {}
    for chunk in chunked(_CARD_IMG_PATH.glob('*.png'), 10):
        files = [discord.File(str(f.resolve())) for f in chunk]
        message = await channel.send(files=files)
        result.update((a.filename.replace('-', ' '), a.url) for a in message.attachments)
    return result


async def save_image_urls(channel, filename=_CARD_IMG_URLS_PATH):
    """Like upload_images but saves the resulting dict into a file."""
    result = await upload_images(channel)
    with open(filename, 'w') as f:
        json.dump(result, f, indent=4, separators=(',', ': '))


def get_card_image_file(card):
    """Return a Path object containing the location of the image of the card."""
    return _CARD_IMG_PATH / f'{card.rank.short}-of-{card.suit.name}s.png'


def get_card_image_url(card):
    """Return the URL of the image of the card."""
    return _card_image_urls.get(str(card))
