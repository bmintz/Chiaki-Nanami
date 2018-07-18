# For future cogs, this module should never be imported. Instead, the bot will
# have a series of properties that are merely thin wrappers around this module

# ----------------------- STUFF ---------------------

# The bot's token, this is NOT to be confused with the "Client Secret"
# KEEP THIS PRIVATE AT ALL COSTS
token = ''

# The API key for Carbonitex. Also keep this private.
carbon_key = ''

# The API key for Discord Bots. Again, keep this private.
bots_key = ''

# The API key for Discord Bot List. As usual, keep this private.
bot_list_key = ''

# The credentials to log into your PostgreSQL database.
# Please keep this private.
psql_user = ''
psql_pass = ''
psql_host = ''
psql_db = ''

# The bot's webhook URL. Keep this private too.
# This can be None or an empty string.
webhook_url = ''

# The destination where all the feedback will be sent. This can be one of three things:
# 1. An integer - representing the ID of the feedback channel
# 2. The URL of the webhook for a channel
# 3. Nothing (blank string or None), representing no channel.
feedback_destination = ''

# Where to redirect users when they need help using the bot.
# This should be an invite link.
support_server_invite = 'https://discord.gg/WtkPTmE'

# -------------------- BOT STUFF ---------------------

# The bot's default command prefix. This can either be a string, 
# or a tuple/list of prefixes
command_prefix = '->'

# The bot's description
description = "I'm Chiaki Nanami, the gamer for gamers!"

# The extensions that will be initially loaded when the bot starts
extensions = []

# The possible games the bot will randomly choose from for the playing status.
# These are not cycled.
#
# To cover the new activity types, an entry in this list can be one of
# three types:
# str = just the name of the game, this will default to the "playing" status.
# tuple/list of (activity_type, name, Optional[url])
# dict of {'type': activity_type, 'name': name}
#
# The activity_type can be one of 4 types:
# * 0 - 'Playing'
# * 1 - 'Streaming' (Requires a twitch.tv url)
# * 2 - 'Listening'
# * 3 - 'Watching'
# You can either use the number (e.g. 2), or the name (e.g. 'listening')
#
# There are a few formats you can put in your playing status. These are:
# {server_count} = how many servers the bot is in
# {user_count} = how many users the bots shares a server with
# {version} = the bot's version number
#
# Note that if you want to have either { or } in your name you have
# to double them up. For e.g. {{status}}
games = []

# Twitch URL, you only need to provide this as a default if you use
# streaming status.
#
# This must be a valid twitch.tv URL, meaning it needs to include
# the https:// part as well
# (e.g. https://twitch.tv/Chiaki)
twitch_url = ''

# whether to ignore messages from all bots
# if False, only messages from Chiaki herself will be ignored
ignore_bots = True

# ----------------------- COLOURS ---------------------

# The default colour the bot will use for embeds
# It must be an integer
# You can use hexadecimal literals for extra readability (eg 0xFFFFFF)
colour = 0

# The colour used for the ok embeds. This colour is used on embeds when 
# something went ok
ok_colour = 0x00FF00

# The colour used for the error embeds. This colour is used on embeds when 
# something went wrong
error_colour = 0xFF000
