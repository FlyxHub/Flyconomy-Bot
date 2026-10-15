"""Tests for the weekly tournament: its rules, its ledger, and its settling."""

from __future__ import annotations

import datetime
import random
from zoneinfo import ZoneInfo

import pytest

from flyconomy import tournament
from flyconomy.cogs.base import BaseCog
from flyconomy.cogs.gambling import Gambling
from flyconomy.cogs.tournament import Tournament
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import InsufficientFundsError
from tests.conftest import ALICE, BOB, CAROL
from tests.test_cog_behavior import FakeBot, FakeContext, FakeUser

ZONE = ZoneInfo("America/Chicago")
FEE = tournament.ENTRY_FEE
LAST_WEEK = "2026-W41"
THIS_WEEK = "2026-W42"
#: A Wednesday inside THIS_WEEK.
WEDNESDAY = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=ZONE)


@pytest.fixture
def settings() -> Settings:
    return Settings(discord_token="placeholder", timezone="America/Chicago")


def make_cog(db: Database, settings: Settings, now: datetime.datetime = WEDNESDAY) -> Tournament:
    """Build the cog without starting its midnight timer, frozen at ``now``."""
    cog = Tournament.__new__(Tournament)
    BaseCog.__init__(cog, FakeBot(db, settings))
    cog._zone = ZONE
    cog._now = lambda: now
    return cog


async def enter(
    db: Database, user: int, week: str = THIS_WEEK, *, net: int = 0, wagered: int = FEE
):
    """Fund a member, enter them, and give them a score."""
    await db.add_bank(user, FEE)
    assert await db.join_tournament(user, week, now=float(user % 1000))
    await db.record_tournament_play(user, wagered, wagered + net)


async def bank(db: Database, user: int) -> int:
    return (await db.get_account(user)).bank


class TestRules:
    def test_a_week_is_named_by_its_iso_week(self):
        assert tournament.week_of(WEDNESDAY) == THIS_WEEK

    @pytest.mark.parametrize(
        "when",
        [
            datetime.datetime(2026, 10, 12, 0, 0, tzinfo=ZONE),  # Monday, first minute
            WEDNESDAY,
            datetime.datetime(2026, 10, 18, 23, 59, tzinfo=ZONE),  # Sunday, last minute
        ],
    )
    def test_the_week_ends_at_the_next_monday_midnight(self, when):
        assert tournament.week_ends(when) == datetime.datetime(2026, 10, 19, tzinfo=ZONE)
        assert tournament.week_of(when) == THIS_WEEK

    def test_week_names_sort_in_time_order(self):
        # The database picks a member's open week with MAX(week).
        assert "2026-W09" < "2026-W10" < "2027-W01"

    @pytest.mark.parametrize("pot", [FEE, 2 * FEE, 37 * FEE])
    def test_the_winner_is_always_paid_less_than_the_fees(self, pot):
        assert 0 < tournament.payout(pot) < pot


class TestJoining:
    async def test_joining_takes_the_fee_from_the_bank(self, db):
        await db.add_bank(ALICE, FEE)
        before = await bank(db, ALICE)

        assert await db.join_tournament(ALICE, THIS_WEEK, now=1.0)

        assert await bank(db, ALICE) == before - FEE
        assert (await db.tournament_standings(THIS_WEEK))[:2] == (FEE, 1)

    async def test_joining_twice_charges_once(self, db):
        await db.add_bank(ALICE, 2 * FEE)
        await db.join_tournament(ALICE, THIS_WEEK, now=1.0)
        before = await bank(db, ALICE)

        assert not await db.join_tournament(ALICE, THIS_WEEK, now=2.0)
        assert await bank(db, ALICE) == before

    async def test_a_fee_the_bank_cannot_cover_enters_nobody(self, db):
        await db.ensure_account(ALICE)
        with pytest.raises(InsufficientFundsError):
            await db.join_tournament(ALICE, THIS_WEEK, now=1.0)
        assert await db.tournament_entry(THIS_WEEK, ALICE) is None


class TestScoring:
    async def test_play_counts_only_for_entrants(self, db):
        await db.record_tournament_play(ALICE, 500, 1_000)
        assert await db.open_tournament_weeks() == []

    async def test_play_adds_net_and_stake(self, db):
        await enter(db, ALICE, wagered=0)
        await db.record_tournament_play(ALICE, 500, 1_000)
        await db.record_tournament_play(ALICE, 300, 0)

        entry = await db.tournament_entry(THIS_WEEK, ALICE)
        assert entry is not None
        assert (entry.net, entry.wagered) == (200, 800)

    async def test_play_lands_on_the_newest_open_week(self, db):
        await enter(db, ALICE, LAST_WEEK, wagered=0)
        await enter(db, ALICE, THIS_WEEK, wagered=0)

        await db.record_tournament_play(ALICE, 100, 300)

        assert (await db.tournament_entry(LAST_WEEK, ALICE)).net == 0
        assert (await db.tournament_entry(THIS_WEEK, ALICE)).net == 200

    async def test_a_coinflip_reports_its_result(self, db, settings):
        await enter(db, ALICE, wagered=0)
        cog = Gambling(FakeBot(db, settings))
        cog.rng = random.Random(1)
        await db.add_wallet(ALICE, 1_000)
        ctx = FakeContext(author=FakeUser(id=ALICE))

        await cog.coinflip.callback(cog, ctx, "heads", 1_000)

        entry = await db.tournament_entry(THIS_WEEK, ALICE)
        assert entry is not None
        assert entry.wagered == 1_000
        assert entry.net in (-1_000, 1_000)

    async def test_standings_rank_by_net_then_earliest_entry(self, db):
        await enter(db, BOB, net=500)
        await enter(db, ALICE, net=500)
        await enter(db, CAROL, net=-100)

        _, _, leaders = await db.tournament_standings(THIS_WEEK)

        # ALICE's id sorts first, so `enter` gave her the earlier join time.
        assert [e.user_id for e in leaders] == [ALICE, BOB, CAROL]


class TestSettling:
    async def test_the_best_qualified_net_takes_the_pot_less_the_cut(self, db):
        await enter(db, ALICE, LAST_WEEK, net=5_000)
        await enter(db, BOB, LAST_WEEK, net=-2_000)
        before = await bank(db, ALICE)

        result = await db.settle_tournament(LAST_WEEK, now=0.0)

        assert result is not None
        assert result.winner_id == ALICE
        assert result.pot == 2 * FEE
        assert result.paid == tournament.payout(2 * FEE)
        assert await bank(db, ALICE) == before + result.paid
        assert await db.open_tournament_weeks() == []

    async def test_a_week_settles_once(self, db):
        await enter(db, ALICE, LAST_WEEK)
        await enter(db, BOB, LAST_WEEK)

        assert await db.settle_tournament(LAST_WEEK, now=0.0) is not None
        assert await db.settle_tournament(LAST_WEEK, now=0.0) is None

    async def test_an_entrant_who_never_played_cannot_win(self, db):
        # Sitting on zero while everyone else drifts below it is not a win.
        await enter(db, ALICE, LAST_WEEK, net=0, wagered=0)
        await enter(db, BOB, LAST_WEEK, net=-500)
        await enter(db, CAROL, LAST_WEEK, net=-900)

        result = await db.settle_tournament(LAST_WEEK, now=0.0)

        assert result is not None
        assert result.winner_id == BOB

    async def test_too_few_qualifiers_refunds_every_fee(self, db):
        await enter(db, ALICE, LAST_WEEK, net=9_000)
        await enter(db, BOB, LAST_WEEK, wagered=0)
        before = (await bank(db, ALICE), await bank(db, BOB))

        result = await db.settle_tournament(LAST_WEEK, now=0.0)

        assert result is not None
        assert result.winner_id is None
        assert (await bank(db, ALICE), await bank(db, BOB)) == (before[0] + FEE, before[1] + FEE)

    async def test_the_last_result_is_kept(self, db):
        await enter(db, ALICE, LAST_WEEK, net=1)
        await enter(db, BOB, LAST_WEEK)
        await db.settle_tournament(LAST_WEEK, now=0.0)

        last = await db.last_tournament_result()

        assert last is not None
        assert (last.week, last.winner_id) == (LAST_WEEK, ALICE)
        assert last.cut == tournament.house_cut(2 * FEE)


class TestAResetForfeitsTheEntry:
    async def test_a_reset_mid_week_cannot_win(self, db):
        await enter(db, ALICE, LAST_WEEK, net=9_000)
        await enter(db, BOB, LAST_WEEK, net=1)
        await enter(db, CAROL, LAST_WEEK, net=0)

        await db.reset_account(ALICE, now=0.0)
        result = await db.settle_tournament(LAST_WEEK, now=0.0)

        assert result is not None
        assert result.winner_id == BOB
        assert result.pot == 2 * FEE


class TestTheCog:
    async def test_only_finished_weeks_are_settled(self, db, settings):
        await enter(db, ALICE, LAST_WEEK, net=1)
        await enter(db, BOB, LAST_WEEK)
        await enter(db, CAROL, THIS_WEEK)
        cog = make_cog(db, settings)

        settled = await cog.settle_due()

        assert [r.week for r in settled] == [LAST_WEEK]
        assert await db.open_tournament_weeks() == [THIS_WEEK]

    async def test_the_house_cut_is_raked_like_any_other(self, db, settings):
        await enter(db, ALICE, LAST_WEEK, net=1)
        await enter(db, BOB, LAST_WEEK)
        pot_before = (await db.lottery_state()).pot

        await make_cog(db, settings).settle_due()

        rake = int(tournament.house_cut(2 * FEE) * settings.lottery_rake)
        assert (await db.lottery_state()).pot == pot_before + rake

    async def test_join_command_enters_this_week(self, db, settings):
        cog = make_cog(db, settings)
        await db.add_bank(ALICE, FEE)
        ctx = FakeContext(author=FakeUser(id=ALICE))

        await cog.tournament_join.callback(cog, ctx)
        await cog.tournament_join.callback(cog, ctx)

        assert await db.tournament_entry(THIS_WEEK, ALICE) is not None
        assert "already" in ctx.last

    async def test_info_shows_the_standings_and_where_you_are(self, db, settings):
        await enter(db, BOB, net=700)
        cog = make_cog(db, settings)
        ctx = FakeContext(author=FakeUser(id=ALICE))

        await cog.tournament_group.callback(cog, ctx)

        embed = ctx.embeds[-1]
        assert f"<@{BOB}>" in embed.description
        assert "+$700" in embed.description
        assert any("/tournament join" in field.value for field in embed.fields)
