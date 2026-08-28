"""Tests for the pure economy rules."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from flyconomy import economy


class TestNetWorth:
    def test_sums_cash_and_the_dollar_value_of_coins(self):
        assert economy.net_worth(wallet=100, bank=900, crypto=2) == 100 + 900 + 20_000

    def test_is_zero_for_a_new_account(self):
        assert economy.net_worth(0, 0, 0) == 0


class TestFlyxcoin:
    def test_cost_scales_with_the_price(self):
        assert economy.flx_cost(3) == 3 * economy.FLX_PRICE

    @pytest.mark.parametrize(
        ("bank", "expected"),
        [(0, 0), (9_999, 0), (10_000, 1), (25_000, 2), (100_000, 10)],
    )
    def test_affordable_coins_round_down(self, bank, expected):
        assert economy.affordable_flx(bank) == expected

    def test_buying_then_selling_is_value_neutral(self):
        coins = economy.affordable_flx(50_000)
        assert economy.flx_cost(coins) == 50_000


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
