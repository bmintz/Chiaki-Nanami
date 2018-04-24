"""Simple utilties for commands in discord.ext.commands

If you want to reuse this code for a custom command class, you can do
so by importing them directly in the class, like so:

from discord.ext import commands

class MyCommand(commands.Command):
    from utils.commands import all_names, walk_parents, all_qualified_names

    # rest of your code here...import
"""

import itertools
import operator

from more_itertools import iterate

__all__ = ['all_names', 'all_qualified_names', 'command_category', 'walk_parents']


def all_names(command):
    """Return a list of all possible names in a command"""
    return [command.name, *command.aliases]

def walk_parents(command):
    """Walk up a command's parent chain."""
    return iter(iterate(operator.attrgetter('parent'), command).__next__, None)

def all_qualified_names(command):
    """Return an iterator of all possible names in a command"""
    return map(' '.join, itertools.product(*map(all_names, reversed(list(walk_parents(command))))))

def command_category(command, default='\u200bOther'):
    """Return the category that a command would fall into, using
    the module the command was defined in.
    """

    # TODO: This assumes a command was defined in a cog in the 'cogs'
    #       directory. Possibly remove that assumption?
    cogs, category, *rest = command.module.split('.', 2)
    return category if rest else default
