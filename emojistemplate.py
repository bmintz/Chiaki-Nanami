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

# ------- Currency Emoji -------------

money = '\N{BANKNOTE WITH DOLLAR SIGN}'


# ------ Numbers -----

# Right now it uses the default key-caps
# However, you may specify custom emojis if needed
#
# Note: The numbers are what *Discord sees them as*. Technically the
# actual keycap number emoji would be {number}\ufe0f\u20e3. But discord
# instead sends it as {number}\u20e3 (without the \ufe0f). Do not add the
# \fe0f in, otherwise it won't send as an actual number.
numbers = [
    f'{n}\u20e3' for n in range(10)
]


# ------- Minesweeper -------

# Not an emoji per se but set to True if you want to be able to use external
# emojis for Minesweeper. This only applies to Minesweeper as this changes
# the control scheme if she's able to use external emojis.
#
# Note that if Chiaki doesn't have Use External Emojis she'll be forced to
# use the default control scheme by default.
msw_use_external_emojis = False

msw_y_row = [
    # Should have emojis representing 1-17.
    # If you set msw_use_external_emojis to True this *must* be filled.
]

msw_letters = [
    # Should have emojis representing A-Q or some equivalent.
    # If you set msw_use_external_emojis to True this *must* be filled.
]

# ------ Connect-Four -------

c4_winning_tiles = [
    '\N{HEAVY BLACK HEART}',
    '\N{BLUE HEART}'
]


# ------- Sudoku ------

sudoku_clues = [
    f'{n}\u20e3' for n in range(1, 9)
]

# ------- Checkers -------

checkers_black_king = '\N{HEAVY BLACK HEART}'
checkers_white_king = '\N{BLUE HEART}'
