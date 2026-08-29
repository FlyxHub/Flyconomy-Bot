"""Blackjack rules.

Like :mod:`flyconomy.economy`, this module imports nothing from ``discord``, so
the whole ruleset is unit tested without a gateway connection. It owns the card
logic and the outcome of a hand; the view layer owns the buttons, and the cog
owns the money.

House rules, all of which are the player-friendly variants:

- The dealer stands on every 17, including a soft 17.
- A natural blackjack pays 3:2.
- The player may double down on the first two cards only.
- Splitting is not offered. It needs several hands in play at once, which the
  single-hand state here deliberately does not model.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

from flyconomy.economy import DECK, Card, draw_cards

#: The number a hand tries not to exceed.
TARGET: Final = 21

#: The dealer draws until reaching this total, then stands.
DEALER_STANDS_ON: Final = 17

#: Cards in an opening hand, and the only hand size that can be a natural.
OPENING_CARDS: Final = 2

#: A natural pays 3:2, so the player gets this fraction of the stake as profit
#: on top of the returned stake. Integer division rounds in the house's favor.
NATURAL_NUMERATOR: Final = 3
NATURAL_DENOMINATOR: Final = 2

#: Rank of an ace in :class:`flyconomy.economy.Card`.
ACE_RANK: Final = 14

#: Value of any face card.
FACE_VALUE: Final = 10

#: How long a member has to act before the hand stands automatically.
DECISION_TIMEOUT_SECONDS: Final = 90


class Outcome(StrEnum):
    """How a hand of blackjack ended, from the player's point of view."""

    PLAYER_BLACKJACK = "player_blackjack"
    PLAYER_WINS = "player_wins"
    DEALER_BUST = "dealer_bust"
    PUSH = "push"
    DEALER_WINS = "dealer_wins"
    PLAYER_BUST = "player_bust"

    @property
    def is_win(self) -> bool:
        """Whether the player finished ahead."""
        return self in {Outcome.PLAYER_BLACKJACK, Outcome.PLAYER_WINS, Outcome.DEALER_BUST}

    @property
    def is_loss(self) -> bool:
        """Whether the player lost the stake."""
        return self in {Outcome.DEALER_WINS, Outcome.PLAYER_BUST}


def card_value(card: Card) -> int:
    """Return a card's blackjack value, counting an ace as 11.

    Args:
        card: The card to value.

    Returns:
        11 for an ace, 10 for a face card, otherwise the rank.
    """
    if card.rank == ACE_RANK:
        return 11
    return min(card.rank, FACE_VALUE)


def _evaluate(cards: Sequence[Card]) -> tuple[int, int]:
    """Return a hand's best total and how many aces still count as 11.

    Aces start at 11 and drop to 1 one at a time, but only while the hand is
    over :data:`TARGET`, which is what makes the total the best available.

    Args:
        cards: The hand.

    Returns:
        A ``(total, aces_counted_high)`` pair.
    """
    total = sum(card_value(card) for card in cards)
    aces_high = sum(1 for card in cards if card.rank == ACE_RANK)
    while total > TARGET and aces_high:
        total -= 10
        aces_high -= 1
    return total, aces_high


def hand_value(cards: Sequence[Card]) -> int:
    """Return the best total a hand can make without busting, if it can."""
    return _evaluate(cards)[0]


def is_soft(cards: Sequence[Card]) -> bool:
    """Return whether the hand holds an ace still counted as 11.

    A soft hand cannot bust on the next card.
    """
    return _evaluate(cards)[1] > 0


def is_bust(cards: Sequence[Card]) -> bool:
    """Return whether the hand is over :data:`TARGET`."""
    return hand_value(cards) > TARGET


def is_blackjack(cards: Sequence[Card]) -> bool:
    """Return whether the hand is a natural: 21 on the opening two cards."""
    return len(cards) == OPENING_CARDS and hand_value(cards) == TARGET


def dealer_should_hit(cards: Sequence[Card]) -> bool:
    """Return whether the dealer must draw again.

    The dealer stands on every 17, soft ones included.
    """
    return hand_value(cards) < DEALER_STANDS_ON


def settle(player: Sequence[Card], dealer: Sequence[Card]) -> Outcome:
    """Decide a finished hand.

    Args:
        player: The player's cards.
        dealer: The dealer's cards, already played out unless the player busted.

    Returns:
        The outcome from the player's point of view.
    """
    player_natural = is_blackjack(player)
    dealer_natural = is_blackjack(dealer)

    if player_natural and dealer_natural:
        return Outcome.PUSH
    if player_natural:
        return Outcome.PLAYER_BLACKJACK
    if dealer_natural:
        return Outcome.DEALER_WINS

    # The player acts first, so a player bust loses even if the dealer would
    # also have busted.
    if is_bust(player):
        return Outcome.PLAYER_BUST
    if is_bust(dealer):
        return Outcome.DEALER_BUST

    player_total = hand_value(player)
    dealer_total = hand_value(dealer)
    if player_total > dealer_total:
        return Outcome.PLAYER_WINS
    if player_total < dealer_total:
        return Outcome.DEALER_WINS
    return Outcome.PUSH


def payout(stake: int, outcome: Outcome) -> int:
    """Return the dollars credited back for a finished hand.

    The stake is debited when the hand is dealt, so this includes it: a push
    returns the stake and an ordinary win returns twice it.

    Args:
        stake: Everything staked on the hand, including a double down.
        outcome: How the hand ended.

    Returns:
        Dollars to credit, which is ``0`` for a loss.
    """
    match outcome:
        case Outcome.PLAYER_BLACKJACK:
            return stake + (stake * NATURAL_NUMERATOR) // NATURAL_DENOMINATOR
        case Outcome.PLAYER_WINS | Outcome.DEALER_BUST:
            return stake * 2
        case Outcome.PUSH:
            return stake
        case _:
            return 0


@dataclass(slots=True)
class Game:
    """One hand of blackjack in progress.

    The hand carries its own shoe, so a card that has been dealt cannot come
    out again. Nothing here touches the database; the caller settles the money
    once :attr:`finished` is true.

    Attributes:
        player: The player's cards.
        dealer: The dealer's cards. The second one is face down until the hand
            ends.
        shoe: The undealt remainder of the deck, drawn from the end.
        stake: Everything staked so far, which a double down doubles.
        doubled: Whether the player doubled down.
        outcome: How the hand ended, or ``None`` while it is still in play.
    """

    player: list[Card]
    dealer: list[Card]
    shoe: list[Card]
    stake: int
    doubled: bool = False
    outcome: Outcome | None = None

    @classmethod
    def deal(cls, stake: int, rng: random.Random | None = None) -> Self:
        """Shuffle a deck and deal an opening hand.

        A natural for either side ends the hand immediately, with no turn for
        the player.

        Args:
            stake: Dollars staked, already debited by the caller.
            rng: Random source, injectable for deterministic tests.

        Returns:
            The dealt hand, which may already be finished.
        """
        shoe = draw_cards(len(DECK), rng)
        player = [shoe.pop(), shoe.pop()]
        dealer = [shoe.pop(), shoe.pop()]

        game = cls(player=player, dealer=dealer, shoe=shoe, stake=stake)
        if is_blackjack(player) or is_blackjack(dealer):
            game.outcome = settle(player, dealer)
        return game

    @property
    def finished(self) -> bool:
        """Whether the hand has been decided."""
        return self.outcome is not None

    @property
    def can_double(self) -> bool:
        """Whether doubling down is still allowed.

        Only on the opening two cards, and only while the hand is live.
        """
        return not self.finished and len(self.player) == OPENING_CARDS

    @property
    def player_value(self) -> int:
        """The player's best total."""
        return hand_value(self.player)

    @property
    def dealer_value(self) -> int:
        """The dealer's best total across every card, face down one included."""
        return hand_value(self.dealer)

    @property
    def dealer_upcard(self) -> Card:
        """The dealer's face-up card."""
        return self.dealer[0]

    def hit(self) -> None:
        """Draw one card for the player.

        Busting ends the hand. Reaching 21 stands automatically, because
        drawing again could only lose.

        Raises:
            RuntimeError: If the hand is already finished.
        """
        self._require_live()
        self.player.append(self._draw())
        if is_bust(self.player) or self.player_value == TARGET:
            self.stand()

    def stand(self) -> None:
        """Play the dealer out and decide the hand.

        The dealer does not draw against a busted player, which is what gives
        the house its edge in this ruleset.

        Raises:
            RuntimeError: If the hand is already finished.
        """
        self._require_live()
        if not is_bust(self.player):
            while dealer_should_hit(self.dealer):
                self.dealer.append(self._draw())
        self.outcome = settle(self.player, self.dealer)

    def double_down(self) -> None:
        """Double the stake, take exactly one card, and stand.

        The caller must debit the extra stake before calling this.

        Raises:
            RuntimeError: If the hand is finished or past the opening two cards.
        """
        if not self.can_double:
            msg = "doubling down is only allowed on the opening two cards"
            raise RuntimeError(msg)
        self.stake *= 2
        self.doubled = True
        self.player.append(self._draw())
        self.stand()

    def _draw(self) -> Card:
        """Take the next card off the shoe.

        Returns:
            The drawn card.

        Raises:
            RuntimeError: If the shoe is empty. One deck cannot realistically
                run out in a single hand, so this means the shoe was built
                wrong rather than that a player drew too much.
        """
        if not self.shoe:
            msg = "the shoe is empty"
            raise RuntimeError(msg)
        return self.shoe.pop()

    def _require_live(self) -> None:
        """Raise if the hand has already been decided.

        Raises:
            RuntimeError: If the hand is finished.
        """
        if self.finished:
            msg = "the hand has already finished"
            raise RuntimeError(msg)
