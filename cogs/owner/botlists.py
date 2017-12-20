import asyncio
import json
import logging

from core.cog import Cog

log = logging.getLogger(__name__)

CARBONITEX_BOTDATA_URL = 'https://www.carbonitex.net/discord/data/botdata.php'
DISCORD_BOTS_API_URL = 'https://bots.discord.pw/api'
DISCORD_BOT_LIST_URL = 'https://discordbots.org/api/'


class Botlists(Cog, hidden=True):
    def __init__(self, bot):
        # Of course...
        super().__init__(bot)

        # Importing the config here so that I can update the config
        # without binding this module to the globals by accident.
        import config

        carbon_key = config.carbon_key
        bots_key = config.bots_key
        bot_list_key = config.bot_list_key
        assert carbon_key or bots_key or bot_list_key, 'no key was specified'

        self._handlers = []
        if carbon_key:
            self._carbon_key = carbon_key
            self._handlers.append(self._update_carbonitex)

        if bots_key:
            self._bots_key = bots_key
            self._handlers.append(self._update_dbots)

        if bot_list_key:
            self._bot_list_key = bot_list_key
            self._handlers.append(self._update_dbl)

    async def _update_carbonitex(self):
        payload = {
            'key': self._carbon_key,
            'servercount': self.bot.guild_count
        }

        async with self.bot.session.post(CARBONITEX_BOTDATA_URL, data=payload) as resp:
            log.info(f'Carbon statistics returned %s for %s', resp.status, payload)

    async def _update_dbots(self):
        payload = json.dumps({
            'server_count': self.bot.guild_count
        })

        headers = {
            'authorization': self._bots_key,
            'content-type': 'application/json'
        }

        url = f'{DISCORD_BOTS_API_URL}/bots/{self.bot.user.id}/stats'
        async with self.bot.session.post(url, data=payload, headers=headers) as resp:
            log.info(f'Discord Bots statistics returned %s for %s', resp.status, payload)

    async def _update_dbl(self):
        headers = {
            'Authorization': self._bot_list_key,
            'Content-Type': 'application/json'
        }

        data = json.dumps({
            'server_count': self.bot.guild_count,
            'shard_count': len(self.bot.shards)
        })

        url = f'{DISCORD_BOT_LIST_URL}/bots/{self.bot.user.id}/stats'
        async with self.bot.session.post(url, headers=headers, data=data) as req:
            log.info(f'Discord Bot List statistics returned %s for %s', req.status, data)

    async def update(self):
        await asyncio.gather(*(handler() for handler in self._handlers))

    async def on_guild_join(self, guild):
        await self.update()

    async def on_guild_remove(self, guild):
        await self.update()

    async def on_ready(self):
        await self.update()


def setup(bot):
    # Importing the config here so that I can update the config
    # without binding this module to the globals by accident.
    import config

    if config.carbon_key or config.bots_key or config.bot_list_key:
        # If neither key was specified then there's really no need
        # to add this cog to the bot. It's pointless as it won't do
        # anything.
        bot.add_cog(Botlists(bot))
