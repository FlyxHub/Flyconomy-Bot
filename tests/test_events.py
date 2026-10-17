"""Tests for server events: the rules they bend, the drop, and the scheduler's state."""

from __future__ import annotations

import random
import time

import pytest

from flyconomy import economy, events
from flyconomy.cogs.base import BaseCog
from flyconomy.cogs.events import Events
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.views import SupplyDropView
from tests.conftest import ALICE, BOB
from tests.test_cog_behavior import FakeBot, FakeChannel, FakeContext, FakeUser, make_economy


@pytest.fixture
def settings() -> Settings:
    return Settings(discord_token="placeholder")


def make_cog(bot: FakeBot) -> Events:
    """Build the cog without starting its scheduler, and register it on the bot."""
    cog = Events.__new__(Events)
    BaseCog.__init__(cog, bot)
    cog.active = None
    bot.cogs["Events"] = cog
    return cog


class TestQuietIsUnchanged:
    """With no event running, the rules draw the same numbers they always did."""

    def test_begging(self):
        for seed in range(200):
            plain, evented = random.Random(seed), random.Random(seed)
            expected = (
                0
                if plain.randint(1, economy.BEG_SUCCESS_ODDS) == 1
                else plain.randint(economy.BEG_MIN, economy.BEG_MAX)
            )
            assert events.roll_beg(evented, None) == expected

    def test_robbing(self):
        for level in range(economy.MAX_SECURITY_LEVEL + 1):
            for seed in range(50):
                plain = economy.roll_rob(level, random.Random(seed))
                assert events.roll_rob(level, None, random.Random(seed)) == plain

    def test_mining(self):
        assert events.mine_yield(1, None) == 1
        assert events.mine_yield(1, events.CRIME_WAVE) == 1


class TestTheBounds:
    def test_charity_never_fails_and_never_pays_past_its_multiple(self):
        rng = random.Random(1)
        for _ in range(2_000):
            amount = events.roll_beg(rng, events.CHARITY_DRIVE)
            assert amount >= economy.BEG_MIN * events.CHARITY_MULTIPLIER
            assert amount <= economy.BEG_MAX * events.CHARITY_MULTIPLIER

    def test_gold_rush_multiplies_a_yield(self):
        assert events.mine_yield(1, events.GOLD_RUSH) == events.GOLD_RUSH_MULTIPLIER
        assert events.mine_yield(0, events.GOLD_RUSH) == 0

    @pytest.mark.parametrize("level", range(economy.MAX_SECURITY_LEVEL + 1))
    def test_a_crime_wave_tilts_robbery_but_never_makes_it_certain(self, level):
        boosted = events.rob_success_percent(level, events.CRIME_WAVE)
        assert boosted == economy.rob_success_percent(level) + events.CRIME_WAVE_BONUS_PERCENT
        assert boosted < 100

    def test_a_drop_stays_in_its_range(self):
        rng = random.Random(2)
        assert all(
            events.DROP_MIN <= events.drop_amount(rng) <= events.DROP_MAX for _ in range(1_000)
        )

    def test_every_event_can_be_picked_and_named(self):
        rng = random.Random(3)
        assert {events.pick(rng) for _ in range(500)} == set(events.ALL)
        assert set(events.BY_KEY) == {"goldrush", "charity", "crimewave", "drop"}


class TestSupplyDrop:
    async def test_only_the_first_grab_is_paid(self, db: Database):
        view = SupplyDropView(db=db, amount=5_000, timezone="UTC")

        assert await view.apply_grab(FakeUser(ALICE))
        assert not await view.apply_grab(FakeUser(BOB))

        assert (await db.get_account(ALICE)).wallet == economy.STARTING_WALLET + 5_000
        assert (await db.find_account(BOB)) is None
        assert (await db.career(ALICE))["drops"] == 1

    async def test_an_expired_drop_pays_nobody(self, db: Database):
        view = SupplyDropView(db=db, amount=5_000, timezone="UTC")

        await view.on_timeout()

        assert view.expired
        assert not await view.apply_grab(FakeUser(ALICE))
        assert await db.find_account(ALICE) is None

    async def test_a_timeout_after_a_grab_changes_nothing(self, db: Database):
        view = SupplyDropView(db=db, amount=5_000, timezone="UTC")
        await view.apply_grab(FakeUser(ALICE))

        await view.on_timeout()

        assert not view.expired
        assert view.winner is not None


class TestScheduler:
    async def test_a_timed_event_runs_until_it_ends(self, db, settings):
        cog = make_cog(FakeBot(db, settings))
        assert cog.current() is None

        await cog.start(events.GOLD_RUSH, FakeContext(FakeUser(ALICE)))
        assert cog.current() is events.GOLD_RUSH

        cog.active = (events.GOLD_RUSH, time.time() - 1)
        assert cog.current() is None

    async def test_a_drop_is_posted_rather_than_run(self, db, settings):
        cog = make_cog(FakeBot(db, settings))
        ctx = FakeContext(FakeUser(ALICE))

        await cog.start(events.SUPPLY_DROP, ctx)

        assert cog.current() is None
        [view] = ctx.views
        assert isinstance(view, SupplyDropView)

    async def test_commands_see_the_running_event(self, db, settings):
        bot = FakeBot(db, settings)
        await make_cog(bot).start(events.CHARITY_DRIVE, FakeChannel())
        economy_cog = make_economy(bot)
        ctx = FakeContext(FakeUser(ALICE))

        for _ in range(20):
            await economy_cog.beg.callback(economy_cog, ctx)
            economy_cog.beg.reset_cooldown(ctx)

        assert not any("nothing" in message for message in ctx.sent)
        assert all("Charity Drive" in message for message in ctx.sent)
