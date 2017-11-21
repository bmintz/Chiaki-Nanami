import argparse
import asyncio
import logging
import sys

from cogs.utils.misc import file_handler
from core import Chiaki

# use faster event loop, but fall back to default if on Windows or not installed
try:
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO)
logger.addHandler(file_handler('discord'))

bot = Chiaki()

#--------------MAIN---------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--create-tables', action='store_true', help='Create the tables before running the bot.')
    args = parser.parse_args()
    if args.create_tables:
        bot.loop.run_until_complete(bot.run_sql())

    bot.run()
    return 69 * bot.reset_requested

if __name__ == '__main__':
    sys.exit(main())
