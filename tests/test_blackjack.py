"""Tests for the blackjack ruleset."""

from __future__ import annotations

import random

import pytest

from flyconomy import blackjack
from flyconomy.blackjack import Outcome
from flyconomy.economy import DECK, Card

ACE = 14
KING = 13
QUEEN = 12
JACK = 11


def card(rank: int, suit: str = "♠") -> Card:
    return Card(rank, suit)


def hand(*ranks: int) -> list[Card]:
    """Build a hand, cycling suits so the cards stay distinct."""
    suits = ["♠", "♥", "♦", "♣"]
    return [Card(rank, suits[index % len(suits)]) for index, rank in enumerate(ranks)]


class TestCardValue:
    @pytest.mark.parametrize("rank", range(2, 11))
    def test_number_cards_are_worth_their_rank(self, rank):
        assert blackjack.card_value(card(rank)) == rank

    @pytest.mark.parametrize("rank", [JACK, QUEEN, KING])
    def test_face_cards_are_worth_ten(self, rank):
        assert blackjack.card_value(card(rank)) == 10

    def test_an_ace_starts_at_eleven(self):
        assert blackjack.card_value(card(ACE)) == 11

    def test_every_card_in_the_deck_has_a_value(self):
        assert all(2 <= blackjack.card_value(c) <= 11 for c in DECK)


class TestHandValue:
    @pytest.mark.parametrize(
        ("ranks", "expected"),
        [
            ((2, 3), 5),
            ((10, 9), 19),
            ((KING, QUEEN), 20),
            ((ACE, KING), 21),
            ((ACE, ACE), 12),
            ((ACE, ACE, ACE), 13),
            ((ACE, ACE, 9), 21),
            ((ACE, 6), 17),
            ((ACE, 6, 10), 17),
            ((ACE, 5, 5), 21),
            ((10, 10, 5), 25),
            ((ACE, ACE, ACE, ACE), 14),
        ],
    )
    def test_totals(self, ranks, expected):
        assert blackjack.hand_value(hand(*ranks)) == expected

    def test_an_ace_only_drops_as_far_as_it_must(self):
        # Two aces and a nine is 21, not 11: only one ace is demoted.
        assert blackjack.hand_value(hand(ACE, ACE, 9)) == 21

    def test_an_empty_hand_is_zero(self):
        assert blackjack.hand_value([]) == 0


class TestSoftHands:
    @pytest.mark.parametrize("ranks", [(ACE, 6), (ACE, KING), (ACE, ACE), (ACE, 2, 3)])
    def test_a_high_ace_makes_a_hand_soft(self, ranks):
        assert blackjack.is_soft(hand(*ranks))

    @pytest.mark.parametrize("ranks", [(10, 7), (ACE, 6, 10), (5, 6, 7), (ACE, KING, QUEEN)])
    def test_a_hand_without_a_high_ace_is_hard(self, ranks):
        assert not blackjack.is_soft(hand(*ranks))

    def test_a_soft_hand_cannot_bust_on_the_next_card(self):
        soft = hand(ACE, 5)
        for extra in DECK:
            assert blackjack.hand_value([*soft, extra]) <= blackjack.TARGET


class TestBustAndBlackjack:
    @pytest.mark.parametrize("ranks", [(10, 10, 5), (KING, QUEEN, JACK), (9, 9, 9)])
    def test_over_21_is_a_bust(self, ranks):
        assert blackjack.is_bust(hand(*ranks))

    @pytest.mark.parametrize("ranks", [(ACE, KING), (10, 10, ACE), (ACE, ACE, 9)])
    def test_21_is_never_a_bust(self, ranks):
        assert not blackjack.is_bust(hand(*ranks))

    def test_21_on_two_cards_is_a_natural(self):
        assert blackjack.is_blackjack(hand(ACE, KING))

    def test_21_on_three_cards_is_not_a_natural(self):
        assert blackjack.hand_value(hand(7, 7, 7)) == 21
        assert not blackjack.is_blackjack(hand(7, 7, 7))

    def test_a_two_card_20_is_not_a_natural(self):
        assert not blackjack.is_blackjack(hand(KING, QUEEN))


class TestDealerPolicy:
    @pytest.mark.parametrize("ranks", [(10, 6), (2, 3), (10, 2, 4)])
    def test_the_dealer_draws_below_17(self, ranks):
        assert blackjack.dealer_should_hit(hand(*ranks))

    @pytest.mark.parametrize("ranks", [(10, 7), (10, 8), (KING, ACE)])
    def test_the_dealer_stands_on_17_or_more(self, ranks):
        assert not blackjack.dealer_should_hit(hand(*ranks))

    def test_the_dealer_stands_on_a_soft_17(self):
        # This house plays S17, the player-friendly variant.
        soft_seventeen = hand(ACE, 6)
        assert blackjack.hand_value(soft_seventeen) == 17
        assert blackjack.is_soft(soft_seventeen)
        assert not blackjack.dealer_should_hit(soft_seventeen)


class TestSettle:
    def test_a_natural_beats_a_plain_21(self):
        assert blackjack.settle(hand(ACE, KING), hand(7, 7, 7)) == Outcome.PLAYER_BLACKJACK

    def test_two_naturals_push(self):
        assert blackjack.settle(hand(ACE, KING), hand(ACE, QUEEN)) == Outcome.PUSH

    def test_a_dealer_natural_beats_a_plain_21(self):
        assert blackjack.settle(hand(7, 7, 7), hand(ACE, KING)) == Outcome.DEALER_WINS

    def test_a_player_bust_loses_even_when_the_dealer_would_also_bust(self):
        # The player acts first, so the dealer never plays. This is the whole
        # source of the house edge.
        assert blackjack.settle(hand(10, 10, 5), hand(10, 10, 5)) == Outcome.PLAYER_BUST

    def test_a_dealer_bust_wins(self):
        assert blackjack.settle(hand(10, 8), hand(10, 6, 9)) == Outcome.DEALER_BUST

    def test_the_higher_total_wins(self):
        assert blackjack.settle(hand(10, 9), hand(10, 8)) == Outcome.PLAYER_WINS
        assert blackjack.settle(hand(10, 7), hand(10, 8)) == Outcome.DEALER_WINS

    def test_equal_totals_push(self):
        assert blackjack.settle(hand(10, 8), hand(9, 9)) == Outcome.PUSH

    def test_every_outcome_is_reachable(self):
        rng = random.Random(0)
        seen = set()
        for _ in range(3_000):
            game = blackjack.Game.deal(10, rng)
            while not game.finished:
                if game.player_value < 17:
                    game.hit()
                else:
                    game.stand()
            seen.add(game.outcome)
        assert seen == set(Outcome)


class TestOutcomeFlags:
    @pytest.mark.parametrize(
        "outcome", [Outcome.PLAYER_BLACKJACK, Outcome.PLAYER_WINS, Outcome.DEALER_BUST]
    )
    def test_winning_outcomes(self, outcome):
        assert outcome.is_win
        assert not outcome.is_loss

    @pytest.mark.parametrize("outcome", [Outcome.DEALER_WINS, Outcome.PLAYER_BUST])
    def test_losing_outcomes(self, outcome):
        assert outcome.is_loss
        assert not outcome.is_win

    def test_a_push_is_neither(self):
        assert not Outcome.PUSH.is_win
        assert not Outcome.PUSH.is_loss


class TestPayout:
    def test_a_natural_pays_three_to_two(self):
        assert blackjack.payout(100, Outcome.PLAYER_BLACKJACK) == 250

    def test_an_odd_natural_stake_rounds_toward_the_house(self):
        # 25 profit on a 15 stake is 22.5, floored to 22, for 37 returned.
        assert blackjack.payout(15, Outcome.PLAYER_BLACKJACK) == 37

    @pytest.mark.parametrize("outcome", [Outcome.PLAYER_WINS, Outcome.DEALER_BUST])
    def test_an_ordinary_win_pays_even_money(self, outcome):
        assert blackjack.payout(100, outcome) == 200

    def test_a_push_returns_the_stake(self):
        assert blackjack.payout(100, Outcome.PUSH) == 100

    @pytest.mark.parametrize("outcome", [Outcome.DEALER_WINS, Outcome.PLAYER_BUST])
    def test_a_loss_returns_nothing(self, outcome):
        assert blackjack.payout(100, outcome) == 0

    def test_every_outcome_has_a_defined_payout(self):
        for outcome in Outcome:
            assert blackjack.payout(100, outcome) >= 0


class TestGameFlow:
    def test_a_deal_gives_two_cards_each(self):
        game = blackjack.Game.deal(100, random.Random(1))
        assert len(game.player) == 2
        assert len(game.dealer) == 2

    def test_dealt_cards_leave_the_shoe(self):
        game = blackjack.Game.deal(100, random.Random(1))
        assert len(game.shoe) == len(DECK) - 4
        dealt = [*game.player, *game.dealer]
        assert not set(dealt) & set(game.shoe)

    def test_a_card_never_appears_twice(self):
        rng = random.Random(5)
        for _ in range(300):
            game = blackjack.Game.deal(10, rng)
            while not game.finished:
                game.hit()
            everything = [*game.player, *game.dealer]
            assert len(everything) == len(set(everything))

    def test_hitting_adds_a_card(self):
        game = _live_game()
        before = len(game.player)
        game.hit()
        assert len(game.player) == before + 1

    def test_busting_ends_the_hand(self):
        game = blackjack.Game(player=hand(10, 6), dealer=hand(9, 7), shoe=hand(KING), stake=100)
        game.hit()
        assert game.finished
        assert game.outcome == Outcome.PLAYER_BUST

    def test_reaching_21_stands_automatically(self):
        game = blackjack.Game(player=hand(10, 6), dealer=hand(9, 7), shoe=hand(4, 5), stake=100)
        game.hit()
        assert game.finished
        assert game.player_value == 21
        # Standing was automatic, so the dealer drew to at least 17.
        assert game.dealer_value >= blackjack.DEALER_STANDS_ON

    def test_standing_plays_the_dealer_to_17(self):
        game = blackjack.Game(player=hand(10, 9), dealer=hand(5, 6), shoe=hand(2, 4), stake=100)
        game.stand()
        assert game.dealer_value >= blackjack.DEALER_STANDS_ON

    def test_the_dealer_does_not_draw_against_a_bust(self):
        game = blackjack.Game(player=hand(10, 6), dealer=hand(2, 3), shoe=hand(KING), stake=100)
        game.hit()
        assert len(game.dealer) == 2
        assert game.outcome == Outcome.PLAYER_BUST

    def test_a_natural_finishes_on_the_deal(self):
        game = blackjack.Game.deal(100, _RiggedDeal([ACE, KING, 9, 7]))
        assert game.finished
        assert game.outcome == Outcome.PLAYER_BLACKJACK

    def test_a_live_hand_is_not_finished(self):
        assert not _live_game().finished

    def test_an_empty_shoe_fails_loudly(self):
        game = blackjack.Game(player=hand(2, 3), dealer=hand(10, 7), shoe=[], stake=100)
        with pytest.raises(RuntimeError, match="shoe is empty"):
            game.hit()

    def test_acting_on_a_finished_hand_is_refused(self):
        game = _live_game()
        game.stand()
        with pytest.raises(RuntimeError, match="already finished"):
            game.hit()
        with pytest.raises(RuntimeError, match="already finished"):
            game.stand()


class TestDoubleDown:
    def test_doubling_is_allowed_on_the_opening_hand(self):
        assert _live_game().can_double

    def test_doubling_doubles_the_stake_and_takes_one_card(self):
        game = blackjack.Game(player=hand(6, 5), dealer=hand(9, 7), shoe=hand(9, 2), stake=100)
        game.double_down()
        assert game.stake == 200
        assert game.doubled
        assert len(game.player) == 3
        assert game.finished

    def test_doubling_is_refused_after_a_hit(self):
        game = _live_game()
        game.hit()
        if not game.finished:
            assert not game.can_double
            with pytest.raises(RuntimeError, match="opening two cards"):
                game.double_down()

    def test_doubling_is_refused_on_a_finished_hand(self):
        game = _live_game()
        game.stand()
        assert not game.can_double
        with pytest.raises(RuntimeError, match="opening two cards"):
            game.double_down()

    def test_a_doubled_hand_is_never_a_natural(self):
        game = blackjack.Game(player=hand(ACE, 5), dealer=hand(9, 7), shoe=hand(5, 2), stake=100)
        game.double_down()
        assert game.outcome != Outcome.PLAYER_BLACKJACK


class TestHouseEdge:
    """Blackjack's edge depends on how the player plays, so these bound it by
    simulation rather than asserting one exact figure."""

    @staticmethod
    def _simulate(strategy, hands: int, seed: int) -> float:
        """Return net profit per unit staked under a hit-or-stand strategy."""
        rng = random.Random(seed)
        staked = 0
        returned = 0
        for _ in range(hands):
            game = blackjack.Game.deal(100, rng)
            while not game.finished:
                if strategy(game):
                    game.hit()
                else:
                    game.stand()
            staked += game.stake
            assert game.outcome is not None
            returned += blackjack.payout(game.stake, game.outcome)
        return (returned - staked) / staked

    def test_mimicking_the_dealer_loses_a_few_percent(self):
        # Copying the dealer's rules is the classic naive strategy. It loses
        # because the player busts first and forfeits immediately.
        edge = -self._simulate(lambda g: g.player_value < 17, hands=40_000, seed=11)
        assert 0.02 < edge < 0.10, f"unexpected house edge {edge:.2%}"

    def test_never_hitting_loses_more(self):
        edge = -self._simulate(lambda _g: False, hands=40_000, seed=12)
        assert edge > 0.04, f"standing on everything should be poor: {edge:.2%}"

    def test_a_simple_strategy_beats_mimicking_the_dealer(self):
        # Stand on 17+, and on 12-16 only when the dealer shows a bust card.
        def simple(game: blackjack.Game) -> bool:
            total = game.player_value
            if total >= 17:
                return False
            if total <= 11:
                return True
            return blackjack.card_value(game.dealer_upcard) >= 7

        naive_edge = -self._simulate(lambda g: g.player_value < 17, hands=40_000, seed=13)
        better_edge = -self._simulate(simple, hands=40_000, seed=13)
        assert better_edge < naive_edge

    def test_the_house_always_keeps_some_edge(self):
        for seed in (1, 2, 3):
            edge = -self._simulate(lambda g: g.player_value < 17, hands=20_000, seed=seed)
            assert edge > 0, f"seed {seed} gave the player an edge"


class _RiggedDeal(random.Random):
    """A random source whose shuffle deals a scripted sequence of ranks.

    Game.deal draws the player's two cards first, then the dealer's, off the end
    of the shoe, so the ranks are reversed here to read in dealing order.
    """

    def __init__(self, ranks: list[int]) -> None:
        super().__init__()
        self._ranks = ranks

    def sample(self, population, k, *, counts=None):
        scripted = hand(*self._ranks)
        rest = [c for c in population if c not in scripted]
        return [*rest[: k - len(scripted)], *reversed(scripted)]


def _live_game() -> blackjack.Game:
    """Return a hand that is dealt but not yet decided."""
    rng = random.Random(0)
    while True:
        game = blackjack.Game.deal(100, rng)
        if not game.finished:
            return game
