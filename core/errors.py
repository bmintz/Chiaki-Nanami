import discord
from discord.ext import commands

class ChiakiException(commands.CommandError):
    """Blanket exception for all exceptions with messages that the bot will say"""

class ChiakiCheckFailure(ChiakiException):
    """Check failure that is thrown when a check fails, and the bot must say it."""

class PrivateMessagesOnly(ChiakiCheckFailure):
    """Exception raised when an operation only works in private message contexts."""

class InvalidUserArgument(ChiakiException):
    """Exception raised when the user inputs an invalid argument, even though conversion is successful."""

class ResultsNotFound(ChiakiException):
    """Exception raised when a search returns some form of "not found" """

def private_message_only(error_msg="This command can only be used in private messages"):
    def predicate(ctx):
        if isinstance(ctx.channel, (discord.GroupChannel, discord.DMChannel)):
            return True
        raise PrivateMessagesOnly(error_msg)
    return commands.check(predicate)