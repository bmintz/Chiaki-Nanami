import argparse
import asyncio
import contextlib
import datetime
import logging
import os
import sys

from core import Chiaki

# use faster event loop, but fall back to default if on Windows or not installed
try:
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


@contextlib.contextmanager
def log():
    logging.getLogger('discord').setLevel(logging.INFO)

    os.makedirs(os.path.join(os.path.dirname(__file__), 'logs'), exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.FileHandler(
        filename=f'logs/chiaki-{datetime.datetime.now()}.log',
        encoding='utf-8',
        mode='w'
    )
    fmt = logging.Formatter('[{asctime}] ({levelname:<7}) {name}: {message}', '%Y-%m-%d %H:%M:%S', style='{')
    handler.setFormatter(fmt)
    root.addHandler(handler)

    try:
        yield
    finally:
        for hdlr in root.handlers[:]:
            hdlr.close()
            root.removeHandler(hdlr)


bot = Chiaki()

#--------------MAIN---------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--create-tables', action='store_true', help='Create the tables before running the bot.')
    args = parser.parse_args()
    if args.create_tables:
        bot.loop.run_until_complete(bot.run_sql())

    with log():
        bot.run()
    return 69 * bot.reset_requested

if __name__ == '__main__':
    sys.exit(main())
