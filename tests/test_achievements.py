"""Tests for achievements: the catalogue, the career counters, and awarding."""

from __future__ import annotations

import time

import pytest

from flyconomy import achievements, economy, embeds
from flyconomy.database import Database
from flyconomy.views import announce_achievements
from tests.conftest import ALICE, BOB
from tests.test_cog_behavior import FakeChannel, FakeUser


class TestCatalogue:
    def test_keys_and_emoji_are_unique(self):
        assert len(achievements.BY_KEY) == len(achievements.ALL)
        assert len({a.emoji for a in achievements.ALL}) == len(achievements.ALL)

    def test_nothing_is_earned_from_nothing(self):
        assert achievements.earned({}) == []

    def test_a_goal_is_earned_at_exactly_its_value(self):
        regular = achievements.BY_KEY["regular"]
        assert regular not in achievements.earned({"plays": regular.goal - 1})
        assert regular in achievements.earned({"plays": regular.goal})

    @pytest.mark.parametrize(
        ("staked", "returned", "multiple"),
        [(100, 0, 0), (100, 999, 9), (100, 1_000, 10), (0, 5, 0)],
    )
    def test_a_multiple_is_whole_times_over_rounded_down(self, staked, returned, multiple):
        assert achievements.best_multiple(staked, returned) == multiple

    def test_the_top_levels_track_the_rules(self):
        # A retuned table should move the badge with it, not strand it.
        assert achievements.BY_KEY["magnate"].goal == economy.MAX_MINER_LEVEL
        assert achievements.BY_KEY["fort_knox"].goal == economy.MAX_SECURITY_LEVEL
        assert achievements.BY_KEY["safecracker"].goal == economy.MAX_SECURITY_LEVEL


class TestCareer:
    async def test_house_play_counts_plays_stakes_and_bests(self, db: Database):
        await db.record_house_play(ALICE, 100, 1_200)
        await db.record_house_play(ALICE, 500, 0)

        assert await db.career(ALICE) == {
            "plays": 2,
            "wagered": 600,
            "best_multiple": 12,
            "best_profit": 1_100,
        }

    async def test_a_robbery_counts_itself_its_bounty_and_the_defense_it_beat(self, db: Database):
        await db.add_wallet(BOB, 1_000)
        await db.add_bank(BOB, 10_000_000)
        for _ in range(3):
            await db.buy_security_upgrade(BOB)
        await db.add_bank(ALICE, 1_000)
        await db.post_bounty(ALICE, BOB, 1_000)

        await db.steal(ALICE, BOB, 100)

        career = await db.career(ALICE)
        assert (career["robberies"], career["bounties"], career["cracked"]) == (1, 1, 3)

    async def test_a_lottery_win_is_counted(self, db: Database):
        await db.award_lottery(ALICE)
        assert (await db.career(ALICE))["lottery_wins"] == 1


class TestClaiming:
    async def test_a_milestone_is_awarded_once(self, db: Database):
        await db.get_account(ALICE)
        await db.record_house_play(ALICE, 100, 0)

        first = await db.claim_achievements(ALICE, time.time())
        again = await db.claim_achievements(ALICE, time.time())

        assert [a.key for a in first] == ["first_bet"]
        assert again == []

    async def test_holdings_count_as_they_stand(self, db: Database):
        await db.add_bank(ALICE, 1_000_000)
        keys = {a.key for a in await db.claim_achievements(ALICE, time.time())}
        assert "millionaire" in keys

    async def test_no_account_earns_nothing(self, db: Database):
        assert await db.claim_achievements(ALICE, time.time()) == []

    async def test_a_reset_keeps_badges_and_career(self, db: Database):
        await db.get_account(ALICE)
        await db.record_house_play(ALICE, 100, 0)
        await db.claim_achievements(ALICE, time.time())

        await db.reset_account(ALICE, time.time())

        assert "first_bet" in await db.earned_achievements(ALICE)
        assert (await db.career(ALICE))["plays"] == 1

    async def test_a_purge_clears_them(self, db: Database):
        await db.get_account(ALICE)
        await db.record_house_play(ALICE, 100, 0)
        await db.claim_achievements(ALICE, time.time())

        await db.purge_user(ALICE)

        assert await db.earned_achievements(ALICE) == {}
        assert await db.career(ALICE) == {}


class TestShown:
    async def test_balance_shows_earned_emoji_in_catalogue_order(self, db: Database):
        account = await db.get_account(ALICE)
        embed = embeds.balance_embed(FakeUser(ALICE), account, "UTC", ["regular", "first_bet"])
        [field] = [f for f in embed.fields if f.name and f.name.startswith("Achievements")]
        assert field.value == "🎲 🎰"
        assert field.name == f"Achievements (2/{len(achievements.ALL)})"

    async def test_an_unlock_is_announced_in_the_channel(self, db: Database):
        await db.get_account(ALICE)
        await db.record_house_play(ALICE, 100, 0)
        channel = FakeChannel()

        await announce_achievements(db, FakeUser(ALICE), channel)
        await announce_achievements(db, FakeUser(ALICE), channel)

        assert len(channel.sent) == 1
        assert "First Bet" in channel.sent[0]
