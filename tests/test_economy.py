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
    def _walk(self, ticks: int, seed: int, start: int | None = None) -> list[economy.MarketState]:
        rng = random.Random(seed)
        state = economy.MarketState(price=start or economy.FLX_PRICE)
        seen = []
        for _ in range(ticks):
            state = economy.next_flx_market(state, rng)
            seen.append(state)
        return seen

    @pytest.mark.parametrize(
        ("seed", "start"),
        [(1, None), (2, economy.FLX_PRICE_FLOOR), (3, economy.FLX_PRICE_CEILING)],
    )
    def test_stays_within_bounds_over_many_ticks(self, seed, start):
        for state in self._walk(10_000, seed, start):
            assert economy.FLX_PRICE_FLOOR <= state.price <= economy.FLX_PRICE_CEILING

    def test_is_deterministic_given_a_seeded_source(self):
        state = economy.MarketState(price=economy.FLX_PRICE)
        first = economy.next_flx_market(state, random.Random(42))
        second = economy.next_flx_market(state, random.Random(42))
        assert first == second

    def test_reverts_toward_the_anchor_on_average(self):
        # Starting pinned at the ceiling, the mean-reverting pull should drag
        # the average of many ticks back down toward FLX_PRICE rather than
        # leaving it parked at the bound.
        prices = [s.price for s in self._walk(5_000, 4, economy.FLX_PRICE_CEILING)]
        assert sum(prices) / len(prices) < economy.FLX_PRICE_CEILING * 0.9

    def test_never_reaches_zero_or_negative(self):
        for state in self._walk(10_000, 5, economy.FLX_PRICE_FLOOR):
            assert state.price >= 1

    def test_a_settled_calm_market_stays_near_the_anchor(self):
        # The point of the regime is that quiet ticks are still quiet: a market
        # that has not run for a while sits close to the anchor, which is what
        # makes a run legible when one does start. Measured only after the
        # reversion has had time to undo the last run, since the recovery after
        # one is calm but deliberately still far from home.
        rng = random.Random(11)
        state = economy.MarketState(price=economy.FLX_PRICE)
        settled = 0
        prices = []
        for _ in range(20_000):
            state = economy.next_flx_market(state, rng)
            settled = settled + 1 if state.regime == "calm" else 0
            if settled > 100:
                prices.append(state.price)

        assert len(prices) > 1_000, "not enough settled ticks to draw a conclusion"
        # The average is the claim; the bound only catches a walk that has come
        # loose from its anchor entirely.
        assert abs(sum(prices) / len(prices) - economy.FLX_PRICE) < economy.FLX_PRICE * 0.02
        assert max(abs(p - economy.FLX_PRICE) for p in prices) < economy.FLX_PRICE * 0.35


class TestFlxRuns:
    def test_a_calm_market_eventually_starts_a_run(self):
        assert any(s.regime != "calm" for s in TestFlxPriceWalk()._walk(20_000, 6))

    def test_a_run_ends_within_its_declared_length(self):
        rng = random.Random(7)
        state = economy.MarketState(price=economy.FLX_PRICE)
        for _ in range(50_000):
            previous = state
            state = economy.next_flx_market(state, rng)
            started = previous.regime == "calm" and state.regime != "calm"
            if started:
                # ticks_left is what remains after this first tick was spent.
                assert state.ticks_left < economy.FLX_RUN_MAX_TICKS
                length = 1
                while state.regime != "calm":
                    state = economy.next_flx_market(state, rng)
                    length += 1
                assert economy.FLX_RUN_MIN_TICKS <= length <= economy.FLX_RUN_MAX_TICKS
                return
        pytest.fail("no run started in 50,000 ticks")

    def test_a_run_moves_the_price_further_than_a_calm_stretch(self):
        # The whole point of the change: a run has to be able to do what raising
        # the calm volatility never could.
        rng = random.Random(8)
        state = economy.MarketState(price=economy.FLX_PRICE)
        calm: list[float] = []
        run: list[float] = []
        for _ in range(60_000):
            previous = state
            state = economy.next_flx_market(state, rng)
            move = abs(state.price - previous.price) / previous.price
            (run if previous.regime != "calm" else calm).append(move)
        assert run, "no run occurred"
        assert sum(run) / len(run) > sum(calm) / len(calm)

    def test_runs_go_both_ways(self):
        seen = {s.regime for s in TestFlxPriceWalk()._walk(60_000, 9)}
        assert {"bull", "bear"} <= seen

    def test_a_bull_run_can_reach_far_above_the_calm_band(self):
        # A season should occasionally see a price a calm market never would.
        peak = max(s.price for s in TestFlxPriceWalk()._walk(105_120, 10))
        assert peak > economy.FLX_PRICE * 1.3

    def test_a_bear_run_can_reach_far_below_the_calm_band(self):
        trough = min(s.price for s in TestFlxPriceWalk()._walk(105_120, 10))
        assert trough < economy.FLX_PRICE * 0.7

    def test_the_price_still_hovers_near_the_anchor_most_of_the_time(self):
        # Runs are the exception, not the weather. If this drops, the market has
        # stopped being a place to park money and become a casino game.
        prices = [s.price for s in TestFlxPriceWalk()._walk(105_120, 12)]
        near = sum(1 for p in prices if economy.FLX_PRICE * 0.9 <= p <= economy.FLX_PRICE * 1.1)
        assert near / len(prices) > 0.75

    def _bull_ticks(self, seed: int, ticks: int = 200_000):
        """Yield ``(before, after)`` for every tick spent inside a bull run."""
        rng = random.Random(seed)
        state = economy.MarketState(price=economy.FLX_PRICE)
        for _ in range(ticks):
            previous = state
            state = economy.next_flx_market(state, rng)
            # The regime is chosen before the shock, so a tick whose *previous*
            # state was already bullish is one the run actually paid for. That
            # misses the tick a run starts on and keeps the one it ends on,
            # which is immaterial to a share measured over thousands of them.
            if previous.regime == "bull":
                yield previous, state

    def test_a_bull_run_mostly_goes_up(self):
        # The point of a run is that it reads as a trend rather than as a choppy
        # market that happens to end higher. A steady drift under a symmetric
        # shock left a third of a bull run's ticks red -- which is exactly what
        # a member watching it would call "not much of a run".
        up = down = 0
        for previous, state in self._bull_ticks(21):
            if state.price > previous.price:
                up += 1
            elif state.price < previous.price:
                down += 1
        assert up + down > 1_000, "not enough bull ticks to draw a conclusion"
        assert down / (up + down) < 0.25

        # But a run must never become a straight line. A bull run that cannot
        # tick against itself is free money: the exit stops being a decision,
        # and every member sells at the same obvious moment.
        assert down > 0

    def test_a_runs_moves_are_mostly_large_ones(self):
        # "Bigger jumps" is a distribution shape, not a wider range. The
        # magnitude is drawn with its mode at the maximum, so most of a run's
        # ticks clear what the calm market could manage at its most extreme --
        # if this drops below half, a run has gone back to being ordinary
        # weather with a bias on it.
        large = total = 0
        for previous, state in self._bull_ticks(22):
            move = abs(state.price - previous.price) / previous.price
            large += move > economy.FLX_VOLATILITY_PERCENT / 100
            total += 1
        assert total > 1_000, "not enough bull ticks to draw a conclusion"
        assert large / total > 0.6

    def test_the_bounds_stay_a_backstop_not_the_mechanism(self):
        # A run decelerates under its own weight because reversion is suppressed
        # during one rather than switched off, so its size falls out of the
        # shock and the length. If most runs instead end up pinned against the
        # ceiling, the bound has quietly become the thing shaping a run, and
        # retuning the shock would stop changing anything a member can see.
        pinned = runs = 0
        peak = 0
        for _, state in self._bull_ticks(23):
            peak = max(peak, state.price)
            if state.regime == "calm":  # the tick this run ended on
                runs += 1
                pinned += peak >= economy.FLX_PRICE_CEILING
                peak = 0
        assert runs > 50, "not enough completed runs to draw a conclusion"
        assert pinned / runs < 0.3

    def test_run_reversion_stays_below_the_calm_pull(self):
        # "Suppressed during a run" is the claim the deceleration argument
        # rests on. Raising it above the calm pull would make a run revert
        # harder than a quiet market, which is incoherent however well it
        # happened to tune.
        assert economy.FLX_RUN_REVERSION_PERCENT < economy.FLX_MEAN_REVERSION_PERCENT
        assert economy.FLX_RUN_REVERSION_PERCENT > 0


class TestFlxBuyAllowance:
    @pytest.mark.parametrize(
        ("bought", "expected"),
        [(0, 100), (40, 60), (100, 0), (140, 0)],
    )
    def test_reports_what_is_left_of_the_day(self, bought, expected):
        assert economy.flx_buy_allowance(bought, 100) == expected

    def test_defaults_to_the_configured_cap(self):
        assert economy.flx_buy_allowance(0) == economy.FLX_DAILY_BUY_CAP


class TestDailyPayout:
    @pytest.mark.parametrize(
        ("bank", "expected"),
        [(0, 0), (5, 0), (1_000, 100), (12_345, 1_234)],
    )
    def test_pays_ten_percent_rounded_down(self, bank, expected):
        assert economy.daily_payout(bank) == expected


class TestTransferSplit:
    def test_the_recipient_and_the_tax_add_back_up_to_the_amount(self):
        split = economy.split_transfer(1_000)
        assert split.net + split.tax == split.amount

    def test_the_two_halves_add_back_up_to_the_tax(self):
        split = economy.split_transfer(1_000)
        assert split.pot_share + split.creator_share == split.tax

    def test_the_default_rate_takes_a_twentieth(self):
        split = economy.split_transfer(1_000)
        assert split.tax == 50
        assert split.net == 950

    def test_the_tax_rounds_up_so_no_transfer_is_free(self):
        # 100 * 0.05 is exactly 5, so pick an amount whose tax has a remainder.
        split = economy.split_transfer(101)
        assert split.tax == 6

    @pytest.mark.parametrize("amount", [100, 101, 999, 1_000, 12_345, 1_000_000])
    def test_a_transfer_never_conjures_or_loses_a_dollar(self, amount):
        split = economy.split_transfer(amount)
        assert split.net + split.pot_share + split.creator_share == amount

    def test_the_odd_dollar_leans_to_the_creator_so_it_is_destroyed_when_unset(self):
        # An odd tax cannot halve evenly. The extra dollar has to fall on one
        # side; the creator's is the side that is destroyed when unconfigured.
        split = economy.split_transfer(100)
        assert split.tax == 5
        assert (split.pot_share, split.creator_share) == (2, 3)

    @pytest.mark.parametrize("amount", [0, 1, 99, -100])
    def test_a_transfer_under_the_floor_is_refused(self, amount):
        with pytest.raises(ValueError, match="at least"):
            economy.split_transfer(amount)

    def test_the_floor_leaves_the_recipient_something_at_any_allowed_rate(self):
        # The settings bound the rate at 0.5; the floor has to survive that.
        split = economy.split_transfer(economy.MIN_TRANSFER, rate=0.5)
        assert split.net > 0

    def test_a_rate_of_zero_delivers_the_whole_amount(self):
        split = economy.split_transfer(1_000, rate=0)
        assert (split.net, split.tax) == (1_000, 0)


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


class TestSelfReset:
    def test_the_first_reset_of_a_chain_is_a_real_fresh_start(self):
        assert economy.reset_seed(0) == economy.STARTING_BANK

    def test_each_reset_in_a_row_seeds_half_of_the_one_before(self):
        seeds = [economy.reset_seed(n) for n in range(economy.RESET_SEED_HALVINGS)]
        assert seeds == [1_000, 500, 250]

    def test_resetting_past_the_schedule_seeds_nothing(self):
        assert all(
            economy.reset_seed(n) == 0
            for n in range(economy.RESET_SEED_HALVINGS, economy.RESET_SEED_HALVINGS + 10)
        )

    def test_no_reset_ever_seeds_more_than_the_one_before_it(self):
        seeds = [economy.reset_seed(n) for n in range(12)]
        assert seeds == sorted(seeds, reverse=True)

    def test_a_member_who_has_never_reset_starts_a_fresh_chain(self):
        assert economy.chained_resets(0, last_reset=None, now=1_000.0) == 0

    def test_resets_close_together_are_one_chain(self):
        assert economy.chained_resets(2, last_reset=1_000.0, now=1_060.0) == 2

    def test_a_full_cycle_since_the_last_reset_breaks_the_chain(self):
        # The whole point: leave it a day and the next reset is worth the full
        # starting bank again, however many were taken before.
        last = 1_000.0
        assert economy.chained_resets(9, last, last + economy.RESET_CYCLE_SECONDS) == 0

    def test_the_chain_survives_right_up_to_the_cycle(self):
        last = 1_000.0
        assert economy.chained_resets(2, last, last + economy.RESET_CYCLE_SECONDS - 1) == 2

    def test_a_broken_chain_seeds_the_full_stake_again(self):
        last = 1_000.0
        chained = economy.chained_resets(3, last, last + economy.RESET_CYCLE_SECONDS)
        assert economy.reset_seed(chained) == economy.STARTING_BANK

    def test_the_wait_for_a_full_stake_counts_down_from_the_last_reset(self):
        remaining = economy.reset_cycle_expires_in(last_reset=100.0, now=1_000.0)
        assert remaining == economy.RESET_CYCLE_SECONDS - 900

    def test_someone_who_has_never_reset_waits_for_nothing(self):
        assert economy.reset_cycle_expires_in(None, now=1_000.0) == 0

    def test_a_clock_that_moved_backwards_does_not_extend_the_wait(self):
        assert economy.reset_cycle_expires_in(last_reset=5_000.0, now=1_000.0) >= 0


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
