from discord.ext import commands

class ChiakiException(commands.CommandError):
    """Blanket exception for all exceptions with messages that the bot will say"""

class InvalidUserArgument(ChiakiException):
    """Exception raised when the user inputs an invalid argument, even though conversion is successful."""

class ResultsNotFound(ChiakiException):
    """Exception raised when a search returns some form of "not found" """
