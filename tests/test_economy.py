"""Tests for the pure economy rules."""

from __future__ import annotations

import itertools
import random
from collections import Counter
from fractions import Fraction
from typing import ClassVar

import pytest

from flyconomy import economy


class TestNetWorth:
    def test_sums_cash_and_the_dollar_value_of_coins(self):
        assert economy.net_worth(wallet=100, bank=900, crypto=2) == 100 + 900 + 20_000

    def test_is_zero_for_a_new_account(self):
        assert economy.net_worth(0, 0, 0) == 0

    def test_defaults_to_the_base_price(self):
        assert economy.net_worth(0, 0, 1) == economy.FLX_PRICE

    def test_uses_a_live_price_when_given_one(self):
        assert economy.net_worth(0, 0, 1, price=5_000) == 5_000


class TestFlyxcoin:
    def test_cost_scales_with_the_price(self):
        assert economy.flx_cost(3) == 3 * economy.FLX_PRICE

    def test_cost_uses_a_live_price_when_given_one(self):
        assert economy.flx_cost(3, price=5_000) == 15_000

    @pytest.mark.parametrize(
        ("bank", "expected"),
        [(0, 0), (9_999, 0), (10_000, 1), (25_000, 2), (100_000, 10)],
    )
    def test_affordable_coins_round_down(self, bank, expected):
        assert economy.affordable_flx(bank) == expected

    def test_affordable_coins_use_a_live_price_when_given_one(self):
        assert economy.affordable_flx(15_000, price=5_000) == 3

    def test_buying_then_selling_is_value_neutral(self):
        coins = economy.affordable_flx(50_000)
        assert economy.flx_cost(coins) == 50_000


class TestFlxPriceWalk:
    def test_stays_within_bounds_over_many_ticks(self):
        rng = random.Random(1)
        price = economy.FLX_PRICE
        for _ in range(10_000):
            price = economy.next_flx_price(price, rng)
            assert economy.FLX_PRICE_FLOOR <= price <= economy.FLX_PRICE_CEILING

    def test_stays_within_bounds_starting_from_the_floor(self):
        rng = random.Random(2)
        price = economy.FLX_PRICE_FLOOR
        for _ in range(1_000):
            price = economy.next_flx_price(price, rng)
            assert economy.FLX_PRICE_FLOOR <= price <= economy.FLX_PRICE_CEILING

    def test_stays_within_bounds_starting_from_the_ceiling(self):
        rng = random.Random(3)
        price = economy.FLX_PRICE_CEILING
        for _ in range(1_000):
            price = economy.next_flx_price(price, rng)
            assert economy.FLX_PRICE_FLOOR <= price <= economy.FLX_PRICE_CEILING

    def test_is_deterministic_given_a_seeded_source(self):
        first = economy.next_flx_price(economy.FLX_PRICE, random.Random(42))
        second = economy.next_flx_price(economy.FLX_PRICE, random.Random(42))
        assert first == second

    def test_reverts_toward_the_anchor_on_average(self):
        # Starting pinned at the ceiling, the mean-reverting pull should drag
        # the average of many ticks back down toward FLX_PRICE rather than
        # leaving it parked at the bound.
        rng = random.Random(4)
        price = economy.FLX_PRICE_CEILING
        samples = []
        for _ in range(5_000):
            price = economy.next_flx_price(price, rng)
            samples.append(price)
        average = sum(samples) / len(samples)
        assert average < economy.FLX_PRICE_CEILING * 0.9

    def test_never_reaches_zero_or_negative(self):
        rng = random.Random(5)
        price = economy.FLX_PRICE_FLOOR
        for _ in range(10_000):
            price = economy.next_flx_price(price, rng)
            assert price >= 1


class TestDailyPayout:
    @pytest.mark.parametrize(
        ("bank", "expected"),
        [(0, 0), (5, 0), (1_000, 100), (12_345, 1_234)],
    )
    def test_pays_ten_percent_rounded_down(self, bank, expected):
        assert economy.daily_payout(bank) == expected


class TestMinerUpgrades:
    @pytest.mark.parametrize(
        ("level", "cost"),
        [(0, 100), (1, 5_000), (2, 20_000), (3, 100_000), (4, 500_000)],
    )
    def test_costs_match_the_documented_table(self, level, cost):
        assert economy.upgrade_cost(level) == cost

    def test_max_level_cannot_upgrade(self):
        assert economy.upgrade_cost(economy.MAX_MINER_LEVEL) is None

    def test_admin_miner_cannot_upgrade(self):
        assert economy.upgrade_cost(economy.ADMIN_MINER_LEVEL) is None

    def test_every_upgradeable_level_leads_to_a_known_chance(self):
        for level in economy.UPGRADE_COST:
            assert economy.mine_chance_percent(level + 1) > 0


class TestWalletSecurity:
    @pytest.mark.parametrize(
        ("level", "cost"),
        [(0, 2_500), (1, 15_000), (2, 60_000), (3, 250_000), (4, 1_000_000)],
    )
    def test_costs_match_the_documented_table(self, level, cost):
        assert economy.security_cost(level) == cost

    def test_max_level_cannot_upgrade(self):
        assert economy.security_cost(economy.MAX_SECURITY_LEVEL) is None

    def test_every_level_costs_more_than_the_one_below(self):
        costs = [economy.SECURITY_COST[level] for level in sorted(economy.SECURITY_COST)]
        assert costs == sorted(costs)
        assert len(set(costs)) == len(costs)

    def test_an_undefended_wallet_is_robbed_as_often_as_it_ever_was(self):
        # 50% is what `rob` paid before security existed. Buying nothing must
        # not make anyone safer than they were.
        assert economy.rob_success_percent(0) == 50

    def test_every_level_is_harder_to_rob_than_the_one_below(self):
        chances = [
            economy.rob_success_percent(level) for level in range(economy.MAX_SECURITY_LEVEL + 1)
        ]
        assert chances == sorted(chances, reverse=True)
        assert len(set(chances)) == len(chances)

    def test_no_level_makes_a_wallet_unrobbable(self):
        # A wallet nobody can rob removes the reason to bank money at all.
        assert all(
            economy.rob_success_percent(level) > 0
            for level in range(economy.MAX_SECURITY_LEVEL + 1)
        )

    @pytest.mark.parametrize("level", [-1, economy.MAX_SECURITY_LEVEL + 1, 999])
    def test_a_level_off_the_table_is_clamped_onto_it(self, level):
        assert economy.rob_success_percent(level) in economy.ROB_SUCCESS_PERCENT.values()

    @pytest.mark.parametrize("level", sorted(economy.ROB_SUCCESS_PERCENT))
    def test_the_roll_matches_the_advertised_percentage(self, level):
        # A fixed seed keeps this deterministic; the tolerance covers sampling
        # noise, not a difference in the underlying rate.
        rng = random.Random(4321)
        trials = 20_000
        hits = sum(economy.roll_rob(level, rng) for _ in range(trials))
        expected = economy.rob_success_percent(level)
        assert abs(hits / trials * 100 - expected) < 1.0


class TestMining:
    def test_no_miner_never_yields(self):
        rng = random.Random(0)
        assert all(economy.roll_mine(0, rng) == 0 for _ in range(200))

    def test_admin_miner_always_yields_the_full_amount(self):
        rng = random.Random(0)
        assert economy.roll_mine(economy.ADMIN_MINER_LEVEL, rng) == economy.ADMIN_MINE_YIELD

    @pytest.mark.parametrize("level", [1, 2, 3, 4, 5])
    def test_yield_is_one_coin_or_none(self, level):
        rng = random.Random(level)
        assert {economy.roll_mine(level, rng) for _ in range(500)} <= {0, 1}

    @pytest.mark.parametrize(("level", "percent"), sorted(economy.MINE_CHANCE_PERCENT.items()))
    def test_success_rate_matches_the_advertised_percentage(self, level, percent):
        # A fixed seed keeps this deterministic; the tolerance covers sampling
        # noise, not a difference in the underlying rate.
        rng = random.Random(1234)
        trials = 20_000
        hits = sum(economy.roll_mine(level, rng) for _ in range(trials))
        assert abs(hits / trials * 100 - percent) < 1.0

    def test_higher_levels_are_strictly_better(self):
        percentages = [economy.mine_chance_percent(level) for level in range(1, 6)]
        assert percentages == sorted(percentages)
        assert len(set(percentages)) == len(percentages)


class TestRockPaperScissors:
    @pytest.mark.parametrize("move", economy.RPS_MOVES)
    def test_the_same_move_is_a_tie(self, move):
        assert economy.rps_outcome(move, move) == "tie"

    @pytest.mark.parametrize(
        ("player", "bot_move"),
        [("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")],
    )
    def test_winning_matchups(self, player, bot_move):
        assert economy.rps_outcome(player, bot_move) == "win"

    @pytest.mark.parametrize(
        ("player", "bot_move"),
        [("rock", "paper"), ("paper", "scissors"), ("scissors", "rock")],
    )
    def test_losing_matchups(self, player, bot_move):
        assert economy.rps_outcome(player, bot_move) == "lose"

    def test_every_matchup_has_a_result(self):
        results = Counter(
            economy.rps_outcome(player, bot_move)
            for player in economy.RPS_MOVES
            for bot_move in economy.RPS_MOVES
        )
        assert results == {"win": 3, "lose": 3, "tie": 3}


class TestRoulette:
    def test_wheel_has_38_distinct_pockets(self):
        assert len(economy.ROULETTE_WHEEL) == 38
        assert len(set(economy.ROULETTE_WHEEL)) == 38

    def test_wheel_has_a_zero_and_a_double_zero(self):
        # Version 1 wrote the literal 00, which Python reads as 0, so its wheel
        # held two zeroes and no double zero.
        assert 0 in economy.ROULETTE_WHEEL
        assert "00" in economy.ROULETTE_WHEEL

    def test_colors_split_the_numbered_pockets_evenly(self):
        assert len(economy.ROULETTE_RED) == 18
        assert len(economy.ROULETTE_BLACK) == 18
        assert not economy.ROULETTE_RED & economy.ROULETTE_BLACK
        assert set(range(1, 37)) == economy.ROULETTE_RED | economy.ROULETTE_BLACK

    @pytest.mark.parametrize(("pocket", "color"), [(1, "red"), (2, "black"), (0, "green")])
    def test_pocket_colors(self, pocket, color):
        assert economy.roulette_color(pocket) == color

    def test_double_zero_is_green(self):
        assert economy.roulette_color("00") == "green"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("red", "red"),
            ("BLACK", "black"),
            ("  Red  ", "red"),
            ("00", "00"),
            ("0", 0),
            ("36", 36),
        ],
    )
    def test_parses_valid_bets(self, raw, expected):
        assert economy.parse_roulette_bet(raw) == expected

    @pytest.mark.parametrize("raw", ["37", "-1", "green", "", "1.5", "rouge"])
    def test_rejects_invalid_bets(self, raw):
        assert economy.parse_roulette_bet(raw) is None

    def test_straight_bet_pays_35x_on_its_pocket(self):
        assert economy.roulette_payout_multiplier(17, 17) == economy.ROULETTE_STRAIGHT_RETURN

    def test_straight_bet_pays_nothing_otherwise(self):
        assert economy.roulette_payout_multiplier(17, 18) == 0

    def test_double_zero_bet_does_not_win_on_zero(self):
        assert economy.roulette_payout_multiplier("00", 0) == 0
        assert economy.roulette_payout_multiplier("00", "00") == economy.ROULETTE_STRAIGHT_RETURN

    def test_color_bet_pays_on_a_matching_pocket(self):
        assert economy.roulette_payout_multiplier("red", 1) == economy.ROULETTE_COLOR_RETURN
        assert economy.roulette_payout_multiplier("black", 2) == economy.ROULETTE_COLOR_RETURN

    def test_color_bet_loses_on_green(self):
        assert economy.roulette_payout_multiplier("red", 0) == 0
        assert economy.roulette_payout_multiplier("black", "00") == 0

    def test_exactly_one_pocket_wins_a_straight_bet(self):
        winners = [
            pocket
            for pocket in economy.ROULETTE_WHEEL
            if economy.roulette_payout_multiplier(7, pocket)
        ]
        assert winners == [7]

    def test_a_color_bet_wins_on_18_of_38_pockets(self):
        winners = sum(
            1
            for pocket in economy.ROULETTE_WHEEL
            if economy.roulette_payout_multiplier("red", pocket)
        )
        assert winners == 18


class TestSlots:
    """The reel is uniform, so all 6**3 spins are equally likely and the return
    is exact rather than sampled."""

    ALL_SPINS: ClassVar = list(itertools.product(economy.SLOT_REEL, repeat=economy.SLOT_REEL_COUNT))

    def test_every_spin_is_enumerated(self):
        assert len(self.ALL_SPINS) == len(economy.SLOT_REEL) ** economy.SLOT_REEL_COUNT
        assert len(self.ALL_SPINS) == 216

    def test_the_house_edge_matches_the_documented_rate(self):
        returned = sum(economy.slots_payout_multiplier(spin) for spin in self.ALL_SPINS)
        rtp = Fraction(returned, len(self.ALL_SPINS))
        # 207/216. If a payout in the table changes, retune it and update the
        # README, rather than loosening this assertion.
        assert rtp == Fraction(207, 216)
        assert float(1 - rtp) == pytest.approx(0.0417, abs=0.0001)

    def test_the_house_keeps_an_edge_but_not_a_punishing_one(self):
        returned = sum(economy.slots_payout_multiplier(spin) for spin in self.ALL_SPINS)
        rtp = returned / len(self.ALL_SPINS)
        assert 0.93 < rtp < 1.0, "slots must favor the house, but stay playable"

    def test_roughly_one_spin_in_six_pays(self):
        wins = sum(1 for spin in self.ALL_SPINS if economy.slots_payout_multiplier(spin))
        assert wins == 36
        assert wins / len(self.ALL_SPINS) == pytest.approx(1 / 6)

    @pytest.mark.parametrize("symbol", economy.SLOT_REEL)
    def test_three_of_a_kind_pays_that_symbol_rate(self, symbol):
        spin = (symbol, symbol, symbol)
        assert economy.slots_payout_multiplier(spin) == symbol.triple_return

    @pytest.mark.parametrize("symbol", [s for s in economy.SLOT_REEL if s.pays_on_pair])
    def test_a_paying_pair_returns_the_pair_rate(self, symbol):
        other = next(s for s in economy.SLOT_REEL if s != symbol)
        assert economy.slots_payout_multiplier((symbol, symbol, other)) == (
            economy.SLOT_PAIR_RETURN
        )

    @pytest.mark.parametrize("symbol", [s for s in economy.SLOT_REEL if not s.pays_on_pair])
    def test_a_non_paying_pair_wins_nothing(self, symbol):
        other = next(s for s in economy.SLOT_REEL if s != symbol)
        assert economy.slots_payout_multiplier((symbol, symbol, other)) == 0

    def test_a_pair_pays_in_any_reel_position(self):
        gem = next(s for s in economy.SLOT_REEL if s.pays_on_pair)
        other = next(s for s in economy.SLOT_REEL if not s.pays_on_pair)
        for spin in ((gem, gem, other), (gem, other, gem), (other, gem, gem)):
            assert economy.slots_payout_multiplier(spin) == economy.SLOT_PAIR_RETURN

    def test_three_reels_can_never_hold_two_paying_pairs(self):
        # The payout function returns on the first pair it finds, which is only
        # correct because two pairs cannot fit in three reels.
        for spin in self.ALL_SPINS:
            pairs = [s for s in set(spin) if s.pays_on_pair and spin.count(s) == 2]
            assert len(pairs) <= 1

    def test_rarer_symbols_pay_more(self):
        returns = [symbol.triple_return for symbol in economy.SLOT_REEL]
        assert returns == sorted(returns)
        assert len(set(returns)) == len(returns)

    def test_a_spin_fills_every_reel_from_the_reel_strip(self):
        spin = economy.spin_slots(random.Random(0))
        assert len(spin) == economy.SLOT_REEL_COUNT
        assert all(symbol in economy.SLOT_REEL for symbol in spin)

    def test_spins_vary(self):
        rng = random.Random(1)
        spins = {economy.spin_slots(rng) for _ in range(200)}
        assert len(spins) > 1


class TestCards:
    def test_a_deck_holds_52_distinct_cards(self):
        assert len(economy.DECK) == 52
        assert len(set(economy.DECK)) == 52

    def test_every_suit_holds_every_rank(self):
        for suit in economy.CARD_SUITS:
            ranks = {card.rank for card in economy.DECK if card.suit == suit}
            assert ranks == set(economy.CARD_RANKS)

    @pytest.mark.parametrize(
        ("rank", "expected"),
        [(2, "2"), (10, "10"), (11, "J"), (12, "Q"), (13, "K"), (14, "A")],
    )
    def test_ranks_render_the_way_players_read_them(self, rank, expected):
        assert str(economy.Card(rank, "♠")).startswith(expected)

    def test_dealing_takes_cards_without_replacement(self):
        hand = economy.draw_cards(52, random.Random(0))
        assert len(set(hand)) == 52

    def test_dealing_more_than_a_deck_is_rejected(self):
        with pytest.raises(ValueError, match="52-card deck"):
            economy.draw_cards(53)

    def test_dealing_nothing_is_allowed(self):
        assert economy.draw_cards(0) == []


class TestWar:
    ALL_HANDS: ClassVar = list(itertools.permutations(economy.DECK, 2))

    def test_a_higher_card_wins(self):
        assert (
            economy.war_payout_multiplier(economy.Card(14, "♠"), economy.Card(13, "♥"))
            == economy.WAR_WIN_RETURN
        )

    def test_a_lower_card_loses(self):
        assert economy.war_payout_multiplier(economy.Card(2, "♠"), economy.Card(3, "♥")) == 0

    def test_an_equal_rank_returns_the_stake(self):
        assert (
            economy.war_payout_multiplier(economy.Card(9, "♠"), economy.Card(9, "♥"))
            == economy.WAR_TIE_RETURN
        )

    def test_suits_never_break_a_tie(self):
        for suit in economy.CARD_SUITS:
            assert (
                economy.war_payout_multiplier(economy.Card(7, "♠"), economy.Card(7, suit))
                == economy.WAR_TIE_RETURN
            )

    def test_the_game_is_exactly_fair(self):
        # Every ordered pair of distinct cards, so this is exact, not sampled.
        net = sum(
            economy.war_payout_multiplier(player, dealer) - 1 for player, dealer in self.ALL_HANDS
        )
        assert net == 0

    def test_wins_and_losses_are_symmetric(self):
        wins = sum(1 for p, d in self.ALL_HANDS if p.rank > d.rank)
        losses = sum(1 for p, d in self.ALL_HANDS if p.rank < d.rank)
        assert wins == losses

    def test_ties_happen_about_one_hand_in_seventeen(self):
        ties = sum(1 for p, d in self.ALL_HANDS if p.rank == d.rank)
        assert ties / len(self.ALL_HANDS) == pytest.approx(3 / 51)

    def test_a_dealt_hand_never_repeats_a_card(self):
        rng = random.Random(3)
        for _ in range(200):
            player, dealer = economy.draw_cards(2, rng)
            assert player != dealer
