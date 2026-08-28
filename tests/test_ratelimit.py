"""Tests for the sliding window rate limiter.

The clock is injected everywhere, so none of this sleeps.
"""

from __future__ import annotations

import pytest

from flyconomy.ratelimit import SlidingWindowLimiter

ALICE = 1
BOB = 2


class TestConfiguration:
    @pytest.mark.parametrize("rate", [0, -1])
    def test_a_non_positive_rate_is_rejected(self, rate):
        with pytest.raises(ValueError, match="rate must be at least 1"):
            SlidingWindowLimiter(rate=rate, per=10)

    @pytest.mark.parametrize("per", [0, -5])
    def test_a_non_positive_window_is_rejected(self, per):
        with pytest.raises(ValueError, match="per must be positive"):
            SlidingWindowLimiter(rate=5, per=per)


class TestBudget:
    def test_the_first_actions_are_allowed(self):
        limiter = SlidingWindowLimiter(rate=3, per=10)
        assert [limiter.acquire(ALICE, now=0) for _ in range(3)] == [0.0, 0.0, 0.0]

    def test_the_action_past_the_budget_is_refused(self):
        limiter = SlidingWindowLimiter(rate=3, per=10)
        for _ in range(3):
            limiter.acquire(ALICE, now=0)
        assert limiter.acquire(ALICE, now=0) == pytest.approx(10)

    def test_the_wait_shrinks_as_the_window_slides(self):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        limiter.acquire(ALICE, now=0)
        assert limiter.acquire(ALICE, now=4) == pytest.approx(6)
        assert limiter.acquire(ALICE, now=9) == pytest.approx(1)

    def test_budget_returns_once_the_window_passes(self):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        limiter.acquire(ALICE, now=0)
        assert limiter.acquire(ALICE, now=10.1) == 0.0

    def test_a_refused_action_is_not_recorded(self):
        # Hammering a limit must not extend it, or a spammer locks themselves
        # out for longer the harder they push.
        limiter = SlidingWindowLimiter(rate=1, per=10)
        limiter.acquire(ALICE, now=0)
        for _ in range(50):
            limiter.acquire(ALICE, now=5)
        assert limiter.acquire(ALICE, now=10.1) == 0.0

    def test_the_window_slides_rather_than_resetting(self):
        # A fixed window would allow a double burst across the boundary.
        limiter = SlidingWindowLimiter(rate=2, per=10)
        limiter.acquire(ALICE, now=9.0)
        limiter.acquire(ALICE, now=9.5)
        assert limiter.acquire(ALICE, now=10.0) > 0

    def test_a_steady_rate_is_never_refused(self):
        limiter = SlidingWindowLimiter(rate=6, per=10)
        for tick in range(100):
            assert limiter.acquire(ALICE, now=tick * 2.0) == 0.0


class TestIsolation:
    def test_members_have_separate_budgets(self):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        limiter.acquire(ALICE, now=0)
        assert limiter.acquire(BOB, now=0) == 0.0

    def test_one_member_cannot_exhaust_another(self):
        limiter = SlidingWindowLimiter(rate=2, per=10)
        for _ in range(10):
            limiter.acquire(ALICE, now=0)
        assert limiter.acquire(BOB, now=0) == 0.0


class TestInspection:
    def test_retry_after_does_not_spend_budget(self):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        for _ in range(5):
            assert limiter.retry_after(ALICE, now=0) == 0.0
        assert limiter.acquire(ALICE, now=0) == 0.0

    def test_asking_about_an_unknown_member_allocates_nothing(self):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        for key in range(100):
            limiter.retry_after(key, now=0)
        assert limiter.tracked == 0

    def test_resetting_restores_the_budget(self):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        limiter.acquire(ALICE, now=0)
        limiter.reset(ALICE)
        assert limiter.acquire(ALICE, now=0) == 0.0

    def test_resetting_an_unknown_member_is_harmless(self):
        SlidingWindowLimiter(rate=1, per=10).reset(ALICE)


class TestMemory:
    def test_pruning_drops_idle_members(self):
        limiter = SlidingWindowLimiter(rate=2, per=10)
        for key in range(50):
            limiter.acquire(key, now=0)
        assert limiter.tracked == 50

        assert limiter.prune(now=100) == 50
        assert limiter.tracked == 0

    def test_pruning_keeps_active_members(self):
        limiter = SlidingWindowLimiter(rate=2, per=10)
        limiter.acquire(ALICE, now=0)
        limiter.acquire(BOB, now=95)

        limiter.prune(now=100)

        assert limiter.tracked == 1
        assert limiter.retry_after(BOB, now=100) == 0.0

    def test_history_per_member_is_bounded_by_the_rate(self):
        limiter = SlidingWindowLimiter(rate=3, per=10_000)
        for tick in range(500):
            limiter.acquire(ALICE, now=tick)
        assert limiter.tracked == 1

    def test_many_members_do_not_grow_without_bound(self):
        limiter = SlidingWindowLimiter(rate=1, per=1)
        for key in range(5_000):
            limiter.acquire(key, now=key)
        # Idle keys are pruned as new ones arrive.
        assert limiter.tracked < 5_000


class TestDefaultClock:
    def test_the_monotonic_clock_is_used_when_no_time_is_given(self):
        limiter = SlidingWindowLimiter(rate=1, per=60)
        assert limiter.acquire(ALICE) == 0.0
        assert limiter.acquire(ALICE) > 0

    def test_retry_after_reads_the_clock_too(self):
        limiter = SlidingWindowLimiter(rate=1, per=60)
        limiter.acquire(ALICE)
        assert limiter.retry_after(ALICE) > 0
