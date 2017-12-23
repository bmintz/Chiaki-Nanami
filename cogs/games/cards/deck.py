import itertools
import random

from collections import deque

from . import card


def _iter_except(func, exception):
    try:
        while True:
            yield func()
    except exception:
        pass


class Deck:
    _ALL_CARDS = tuple(map(card.Card._make, itertools.product(card.Rank, card.Suit)))

    def __init__(self):
        # We're using a deque to emulate the act of putting used cards on
        # the bottom rather than on the top.
        self.stack = deque(self._ALL_CARDS)
        self.shuffle()

    def __len__(self):
        return len(self.stack)

    def __iter__(self):
        return iter(self.stack)

    def put(self, *cards):
        self.stack.extend(cards)

    def fill(self):
        """Fill the deck with any cards that aren't in the deck already"""
        self.stack.extend(itertools.filterfalse(frozenset(self).__contains__, self._ALL_CARDS))

    def draw(self, n=1):
        """Draws n amount of cards from the deck.

        If n is greater than the number of cards in the deck, it doesn't raise
        an error. Instead, it returns what cards are still left in the deck.
        """
        return list(itertools.islice(_iter_except(self.stack.popleft, IndexError), n))

    def draw_one(self, *, fill=False):
        """Draw one card from the deck.

        If fill is True, it will fill the deck if it's empty.
        Otherwise raise IndexError.
        """
        try:
            return self.draw()[0]
        except IndexError:
            if not fill:
                raise
            self.fill()
            self.shuffle()
            return self.draw()[0]

    def shuffle(self):
        """Shuffle the deck in-place"""
        random.shuffle(self.stack)
