"""Config file for the emojis that Chiaki will use.

RENAME THIS FILE TO emojis.py OTHERWISE IT WON'T WORK.

By default it uses the unicode emojis. However, you can specify 
server emojis in one of the two possible ways:

1. The raw string of the emoji. This is the format <:name:id>. You can find this
   by placing a backslash before the emoji.
2. The ID of the emoji. This must be an integer. And can be shown through the
   same method.

Note that bots have "nitro" status when it comes to emojis. So as long as
it's in the server that has the custom emoji, the bot can use it on any other
server it's in.
"""

# ------- Confirmation emojis (for Context.ask_confirmation) -------

# Confirm option
confirm = '\N{WHITE HEAVY CHECK MARK}'
# Deny Option
deny = '\N{CROSS MARK}'

# ------- Status emojis (for various info commands) ----------------

online = '\N{GREEN HEART}'
idle = '\N{YELLOW HEART}'
dnd = '\N{HEAVY BLACK HEART}'
offline = '\N{BLACK HEART}'
streaming = '\N{PURPLE HEART}'
bot_tag = '\N{ROBOT FACE}'
