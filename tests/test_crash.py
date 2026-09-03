"""Tests for the crash ruleset."""

from __future__ import annotations

import random

import pytest

from flyconomy import crash


class TestMultiplierAt:
    def test_the_multiplier_starts_at_one(self):
        assert crash.multiplier_at(0.0) == pytest.approx(1.0)

    def test_the_multiplier_grows_by_the_configured_rate_each_second(self):
        assert crash.multiplier_at(1.0) == pytest.approx(crash.GROWTH_PER_SECOND)
        assert crash.multiplier_at(2.0) == pytest.approx(crash.GROWTH_PER_SECOND**2)


class TestCrashTimeSeconds:
    def test_an_instant_bust_crashes_at_time_zero(self):
        game = crash.Game(stake=100, crash_point=1.0)
        assert crash.crash_time_seconds(game) == pytest.approx(0.0)

    def test_the_crash_time_matches_the_growth_rate(self):
        game = crash.Game(stake=100, crash_point=crash.GROWTH_PER_SECOND**3)
        assert crash.crash_time_seconds(game) == pytest.approx(3.0)


class TestHasCrashed:
    def test_not_yet_crashed_just_before_the_crash_time(self):
        game = crash.Game(stake=100, crash_point=crash.GROWTH_PER_SECOND**5)
        assert not crash.has_crashed(game, 4.999)

    def test_crashed_at_or_after_the_crash_time(self):
        game = crash.Game(stake=100, crash_point=crash.GROWTH_PER_SECOND**5)
        assert crash.has_crashed(game, 5.0)
        assert crash.has_crashed(game, 10.0)


class TestCurrentMultiplier:
    def test_climbs_with_elapsed_time_while_live(self):
        game = crash.Game(stake=100, crash_point=10.0)
        assert crash.current_multiplier(game, 1.0) == pytest.approx(crash.GROWTH_PER_SECOND)

    def test_freezes_at_the_crash_point_once_busted(self):
        game = crash.Game(stake=100, crash_point=2.0)
        crash_time = crash.crash_time_seconds(game)
        assert crash.current_multiplier(game, crash_time + 10.0) == pytest.approx(2.0)


class TestPayout:
    def test_a_bust_pays_nothing(self):
        assert crash.payout(100, 0.0) == 0

    def test_a_cashout_pays_the_multiplier(self):
        assert crash.payout(100, 2.0) == 200

    def test_rounding_favors_the_house(self):
        # 1.5x on a 15 stake is 22.5, floored to 22.
        assert crash.payout(15, 1.5) == 22


class TestDeal:
    def test_the_stake_is_recorded(self):
        game = crash.Game.deal(500, random.Random(1))
        assert game.stake == 500

    def test_the_crash_point_is_never_below_one(self):
        rng = random.Random(2)
        for _ in range(10_000):
            assert crash.Game.deal(100, rng).crash_point >= 1.0

    def test_the_crash_point_never_exceeds_the_cap(self):
        rng = random.Random(3)
        for _ in range(10_000):
            assert crash.Game.deal(100, rng).crash_point <= crash.MAX_MULTIPLIER

    def test_instant_busts_occur_at_about_the_house_edge_rate(self):
        rng = random.Random(4)
        rounds = 200_000
        instant = sum(1 for _ in range(rounds) if crash.Game.deal(100, rng).crash_point == 1.0)
        assert instant / rounds == pytest.approx(crash.HOUSE_EDGE, abs=0.01)

    @pytest.mark.parametrize("target", [1.5, 2.0, 5.0, 10.0])
    def test_the_survival_rate_matches_the_closed_form(self, target):
        # P(crash_point >= target) should be (1 - HOUSE_EDGE) / target -- this
        # is a regression guard on the sampler, the proof lives in Game.deal.
        rng = random.Random(5)
        rounds = 300_000
        survivors = sum(1 for _ in range(rounds) if crash.Game.deal(100, rng).crash_point >= target)
        expected = (1 - crash.HOUSE_EDGE) / target
        assert survivors / rounds == pytest.approx(expected, abs=0.01)


class TestHouseEdge:
    """Every fixed cash-out target realizes the same flat house edge -- unlike
    blackjack, crash's edge does not depend on strategy. See ``Game.deal`` for
    the proof this simulation is checking."""

    @staticmethod
    def _simulate(target: float, rounds: int, seed: int) -> float:
        """Return net profit per unit staked, always cashing out at ``target``."""
        rng = random.Random(seed)
        staked = 0
        returned = 0
        for _ in range(rounds):
            game = crash.Game.deal(100, rng)
            staked += game.stake
            if game.crash_point >= target:
                returned += crash.payout(game.stake, target)
        return (returned - staked) / staked

    @pytest.mark.parametrize("target", [1.5, 2.0, 5.0, 10.0])
    def test_the_edge_is_flat_across_targets(self, target):
        edge = -self._simulate(target, rounds=300_000, seed=21)
        assert edge == pytest.approx(crash.HOUSE_EDGE, abs=0.02), (
            f"target {target}x gave house edge {edge:.2%}"
        )

    def test_the_house_always_keeps_some_edge(self):
        for seed in (1, 2, 3):
            edge = -self._simulate(2.0, rounds=100_000, seed=seed)
            assert edge > 0, f"seed {seed} gave the player an edge"
