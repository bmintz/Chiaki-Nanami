This is a "simple" card library for Python 3.5+.

To use the library, just `import cards`.

### Examples
```py
import cards
card = cards.Card(cards.Rank.ace, cards.Suit.spade)
print(card)
```

Using a deck to draw out multiple cards

```py
import cards
deck = cards.deck()
card = deck.draw_one()
hand = deck.draw(5)
```