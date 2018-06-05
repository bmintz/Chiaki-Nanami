"""Database utilities for Chiaki

Despite appearances, while this tries to be an ORM, a fully fledged ORM
is not the goal. It's too complex of a project to even bother with one.
Queries will still be made using raw SQL.

The main purpose is to create a self-documenting table class that can be
used as reference and to make creation and migrations much easier.
"""

from .column import *
from .table import *
from .misc import *

__author__ = 'Ikusaba-san'
__license__ = 'MIT'
