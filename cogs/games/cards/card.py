import collections
import enum

__all__ = ['Rank', 'Suit', 'Card']

SHORT_RANKS = ' 23456789TJQKA'
RANK_EMOJIS = [
    None,
    *map('{0}\u20e3'.format, range(2, 10)),
    '\N{KEYCAP TEN}',
    '\N{REGIONAL INDICATOR SYMBOL LETTER J}',
    '\N{REGIONAL INDICATOR SYMBOL LETTER Q}',
    '\N{REGIONAL INDICATOR SYMBOL LETTER K}',
    '\N{REGIONAL INDICATOR SYMBOL LETTER A}',
]


class Rank(enum.Enum):
    two = enum.auto()
    three = enum.auto()
    four = enum.auto()
    five = enum.auto()
    six = enum.auto()
    seven = enum.auto()
    eight = enum.auto()
    nine = enum.auto()
    ten = enum.auto()
    jack = enum.auto()
    queen = enum.auto()
    king = enum.auto()
    ace = enum.auto()

    @property
    def short(self):
        return SHORT_RANKS[self.value]

    @property
    def emoji(self):
        return RANK_EMOJIS[self.value]


class Suit(enum.Enum):
    spade = enum.auto()
    heart = enum.auto()
    diamond = enum.auto()
    club = enum.auto()

    @property
    def emoji(self):
        return SUIT_EMOJIS[self]

SUIT_EMOJIS = dict(zip(Suit, ['\u2660', '\u2764', '\u2666', '\u2663']))


class Card(collections.namedtuple('Card', 'rank suit')):
    """The card object."""
    __slots__ = ()

    def __str__(self):
        return f'{self.rank.short} of {self.suit.name}s'

    def to_unicode(self):
        """Return the emoji representation of the card"""
        return f'{self.rank.emoji} {self.suit.emoji}'

    to_emoji = to_unicode
