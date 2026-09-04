"""Tests for the jackpot: its rules, its storage, and the round that runs it.

The rules are pure, so most of the fairness argument is checked by arithmetic
rather than by simulation. The round itself is driven through the view's
``apply_*`` coroutines against a real database, with a controllable clock in
place of the countdown, so a whole round runs without a gateway or a delay.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from flyconomy import jackpot
from flyconomy.cogs.gambling import Gambling
from flyconomy.config import Settings
from flyconomy.database import Database, JackpotState
from flyconomy.errors import BetTooLargeError, InsufficientFundsError
from flyconomy.ratelimit import SlidingWindowLimiter
from flyconomy.views import JackpotView
from tests.conftest import ALICE, BOB, CAROL, make_v1_database
from tests.test_cog_behavior import FakeBot, FakeContext, FakeUser
from tests.test_views import FakeInteraction, _FakeClock

ANTE = 1_000

#: Ways a pot can be split between entrants, from an even two-way pot to one
#: whale against a minnow. Every one of them has to pay the same edge.
SPLITS = [(100, 100), (100, 900), (1, 1_000_000), (500, 300, 200), (10_000,) * 8]


class FixedTicket(random.Random):
    """A random source that always draws the same ticket from the pot."""

    def __init__(self, ticket: int) -> None:
        super().__init__()
        self.ticket = ticket

    def randrange(self, *_args, **_kwargs) -> int:
        return self.ticket


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(tmp_path / "bot.db")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(discord_token="placeholder")


@pytest.fixture
def ctx() -> FakeContext:
    return FakeContext(author=FakeUser(id=ALICE))


def entries(*pairs: tuple[int, int]) -> tuple[jackpot.Entry, ...]:
    """Build entries from ``(user_id, amount)`` pairs."""
    return tuple(jackpot.Entry(user_id, amount) for user_id, amount in pairs)


def make_view(
    db: Database,
    state: JackpotState,
    *,
    ante: int = ANTE,
    clock: _FakeClock | None = None,
    rng: random.Random | None = None,
    rake: float = 0.0,
    creator_tax_rate: float = 0.0,
    creator_tax_user_id: int | None = None,
    limiter: SlidingWindowLimiter | None = None,
) -> JackpotView:
    """Build a view around a round that already holds its opening ante."""
    return JackpotView(
        db=db,
        rng=rng if rng is not None else random.Random(7),
        state=state,
        ante=ante,
        timezone="UTC",
        rake=rake,
        creator_tax_rate=creator_tax_rate,
        creator_tax_user_id=creator_tax_user_id,
        limiter=limiter,
        now=clock if clock is not None else _FakeClock(),
    )


async def open_round(db: Database, *pairs: tuple[int, int]) -> JackpotState:
    """Fund each member and ante them into the open round."""
    for user_id, amount in pairs:
        await db.add_wallet(user_id, amount)
        await db.enter_jackpot(user_id, amount)
    return await db.jackpot_state()


class TestRules:
    def test_the_house_keeps_its_cut_of_the_pot(self):
        assert jackpot.house_cut(10_000) == int(10_000 * jackpot.HOUSE_CUT)

    def test_the_payout_is_the_pot_less_the_cut(self):
        assert jackpot.payout(10_000) == 10_000 - jackpot.house_cut(10_000)

    @pytest.mark.parametrize("pot", [0, 1, 19, 100, 3_333, 1_000_000, 99_999_999])
    def test_a_round_never_pays_out_more_than_was_anted(self, pot):
        assert jackpot.payout(pot) <= pot

    def test_rounding_the_cut_down_favours_the_players(self):
        # A pot of 19 owes the house 0.95 of a dollar, which truncates to none.
        assert jackpot.house_cut(19) == 0
        assert jackpot.payout(19) == 19

    def test_the_odds_are_the_share_of_the_pot(self):
        assert jackpot.win_chance(250, 1_000) == 0.25

    def test_an_empty_pot_has_no_odds(self):
        assert jackpot.win_chance(0, 0) == 0.0

    def test_the_pot_is_the_sum_of_the_antes(self):
        assert jackpot.total_pot(entries((ALICE, 100), (BOB, 250))) == 350

    def test_a_draw_needs_more_than_one_entrant(self):
        assert jackpot.MIN_ENTRANTS >= 2

    def test_drawing_from_an_empty_pot_is_refused(self):
        with pytest.raises(ValueError, match="empty pot"):
            jackpot.draw_winner((), random.Random(1))

    @pytest.mark.parametrize(
        ("ticket", "expected"),
        [(0, ALICE), (99, ALICE), (100, BOB), (399, BOB), (400, CAROL), (499, CAROL)],
    )
    def test_every_ticket_lands_on_the_ante_that_bought_it(self, ticket, expected):
        # Alice holds 0-99, Bob 100-399, Carol 400-499.
        pot = entries((ALICE, 100), (BOB, 300), (CAROL, 100))
        assert jackpot.draw_winner(pot, FixedTicket(ticket)) == expected

    def test_the_draw_is_weighted_by_ante(self):
        pot = entries((ALICE, 1), (BOB, 9))
        rng = random.Random(2024)
        wins = sum(jackpot.draw_winner(pot, rng) == BOB for _ in range(20_000))
        assert wins / 20_000 == pytest.approx(0.9, abs=0.02)


class TestEveryEntrantFacesTheSameEdge:
    """The property that makes weighting by ante fair: an entrant's expected
    return is their ante less the house's cut, whatever they anted and however
    many others entered. No ante size buys better odds than any other."""

    @pytest.mark.parametrize("antes", SPLITS)
    def test_the_expected_return_is_the_ante_less_the_cut(self, antes):
        pot = sum(antes)
        for ante in antes:
            ev = jackpot.win_chance(ante, pot) * jackpot.payout(pot)
            assert ev == pytest.approx(ante * (1 - jackpot.HOUSE_CUT), abs=1)

    @pytest.mark.parametrize("antes", SPLITS)
    def test_no_entrant_has_a_positive_edge(self, antes):
        pot = sum(antes)
        for ante in antes:
            profit = jackpot.win_chance(ante, pot) * jackpot.payout(pot) - ante
            assert profit <= 0, f"an ante of {ante} in a pot of {pot} profits {profit:+.2f}"

    def test_a_whale_and_a_minnow_face_the_same_edge(self):
        pot = 1_000_100
        whale = jackpot.win_chance(1_000_000, pot) * jackpot.payout(pot) / 1_000_000
        minnow = jackpot.win_chance(100, pot) * jackpot.payout(pot) / 100
        assert whale == pytest.approx(minnow)


class TestMigration:
    async def test_a_version_1_database_gains_the_jackpot(self, tmp_path):
        path = tmp_path / "bot.db"
        make_v1_database(path, [(500, 9_000, 2, 3, ALICE)])

        database = await Database.connect(path)
        try:
            account = await database.get_account(ALICE)
            state = await database.jackpot_state()
        finally:
            await database.close()

        assert (account.wallet, account.bank, account.crypto, account.miner) == (500, 9_000, 2, 3)
        assert (state.round_number, state.pot, state.entrants) == (1, 0, 0)

    async def test_the_round_row_is_a_singleton(self, db, tmp_path):
        connection = sqlite3.connect(tmp_path / "bot.db")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO jackpot (id, round) VALUES (2, 1)")
            count = connection.execute("SELECT COUNT(*) FROM jackpot").fetchone()[0]
        finally:
            connection.close()
        assert count == 1

    async def test_an_ante_of_nothing_is_rejected_by_the_schema(self, db, tmp_path):
        connection = sqlite3.connect(tmp_path / "bot.db")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO jackpot_entries (round, user, amount) VALUES (1, ?, 0)", (ALICE,)
                )
        finally:
            connection.close()

    async def test_a_member_can_only_hold_one_ante_per_round(self, db, tmp_path):
        await open_round(db, (ALICE, ANTE))
        connection = sqlite3.connect(tmp_path / "bot.db")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO jackpot_entries (round, user, amount) VALUES (1, ?, 5)", (ALICE,)
                )
        finally:
            connection.close()

    async def test_migrating_twice_keeps_the_open_round(self, db_path):
        first = await Database.connect(db_path)
        await first.add_wallet(ALICE, ANTE)
        await first.enter_jackpot(ALICE, ANTE)
        await first.close()

        second = await Database.connect(db_path)
        try:
            assert (await second.jackpot_state()).pot == ANTE
        finally:
            await second.close()


class TestStorage:
    async def test_an_ante_leaves_the_wallet_and_lands_in_the_pot(self, db):
        await db.add_wallet(ALICE, 5_000)

        assert await db.enter_jackpot(ALICE, ANTE) is True

        assert (await db.get_account(ALICE)).wallet == 4_000
        assert (await db.jackpot_state()).pot == ANTE

    async def test_a_second_ante_from_the_same_member_is_refused(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.enter_jackpot(ALICE, ANTE)

        assert await db.enter_jackpot(ALICE, ANTE) is False

        # Refused, not charged: the table limit is only meaningful if a member
        # cannot step past it one top-up at a time.
        assert (await db.get_account(ALICE)).wallet == 4_000
        assert (await db.jackpot_state()).pot == ANTE

    async def test_an_ante_beyond_the_wallet_is_refused_and_costs_nothing(self, db):
        await db.add_wallet(ALICE, 100)

        with pytest.raises(InsufficientFundsError):
            await db.enter_jackpot(ALICE, ANTE)

        assert (await db.get_account(ALICE)).wallet == 100
        assert (await db.jackpot_state()).entrants == 0

    async def test_an_ante_of_nothing_is_refused(self, db):
        with pytest.raises(ValueError, match="positive"):
            await db.enter_jackpot(ALICE, 0)

    async def test_the_state_keeps_entrants_in_the_order_they_joined(self, db):
        await open_round(db, (ALICE, 100), (BOB, 200), (CAROL, 300))

        state = await db.jackpot_state()

        assert [entry.user_id for entry in state.entries] == [ALICE, BOB, CAROL]
        assert state.pot == 600
        assert state.entrants == 3

    async def test_awarding_pays_the_winner_and_opens_the_next_round(self, db):
        await open_round(db, (ALICE, 400), (BOB, 600))

        paid = await db.award_jackpot(BOB, cut=50)

        assert paid == 950
        assert (await db.get_account(BOB)).wallet == 950
        state = await db.jackpot_state()
        assert (state.round_number, state.entrants) == (2, 0)

    async def test_awarding_a_cut_larger_than_the_pot_is_refused(self, db):
        await open_round(db, (ALICE, 400), (BOB, 600))

        with pytest.raises(ValueError, match="does not fit"):
            await db.award_jackpot(BOB, cut=1_001)

        assert (await db.jackpot_state()).pot == 1_000

    async def test_refunding_hands_every_ante_back(self, db):
        await open_round(db, (ALICE, 400), (BOB, 600))

        refunded = await db.refund_jackpot()

        assert refunded == list(entries((ALICE, 400), (BOB, 600)))
        assert (await db.get_account(ALICE)).wallet == 400
        assert (await db.get_account(BOB)).wallet == 600
        state = await db.jackpot_state()
        assert (state.round_number, state.entrants) == (2, 0)

    async def test_refunding_nothing_leaves_the_round_alone(self, db):
        assert await db.refund_jackpot() == []
        assert (await db.jackpot_state()).round_number == 1

    async def test_purging_a_member_takes_their_ante_out_of_the_pot(self, db):
        await open_round(db, (ALICE, 400), (BOB, 600))

        result = await db.purge_user(ALICE)

        assert result.jackpot_entries == 1
        assert result.found is True
        state = await db.jackpot_state()
        assert state.entrants == 1
        assert state.pot == 600


class TestRound:
    async def test_joining_adds_the_ante_to_the_pot(self, db):
        state = await open_round(db, (ALICE, ANTE))
        view = make_view(db, state)
        await db.add_wallet(BOB, ANTE)

        assert await view.apply_join(BOB, ANTE) is None

        assert view.state.pot == 2 * ANTE
        assert view.state.entrants == 2

    async def test_joining_twice_is_refused(self, db):
        state = await open_round(db, (ALICE, ANTE))
        view = make_view(db, state)
        await db.add_wallet(ALICE, ANTE)

        problem = await view.apply_join(ALICE, ANTE)

        assert problem is not None
        assert "already in this round" in problem

    async def test_joining_beyond_the_wallet_is_refused(self, db):
        state = await open_round(db, (ALICE, ANTE))
        view = make_view(db, state)

        problem = await view.apply_join(BOB, ANTE)

        assert problem is not None
        assert "wallet" in problem
        assert view.state.entrants == 1

    async def test_a_closed_round_takes_no_more_antes(self, db):
        clock = _FakeClock()
        state = await open_round(db, (ALICE, ANTE))
        view = make_view(db, state, clock=clock)
        await db.add_wallet(BOB, ANTE)

        clock.advance(jackpot.ROUND_SECONDS)

        problem = await view.apply_join(BOB, ANTE)
        assert problem is not None
        assert "already closed" in problem
        assert (await db.get_account(BOB)).wallet == ANTE

    async def test_the_countdown_runs_down_to_zero(self, db):
        clock = _FakeClock()
        view = make_view(db, await open_round(db, (ALICE, ANTE)), clock=clock)

        assert view.seconds_left() == jackpot.ROUND_SECONDS
        clock.advance(jackpot.ROUND_SECONDS / 2)
        assert view.seconds_left() == pytest.approx(jackpot.ROUND_SECONDS / 2)
        clock.advance(jackpot.ROUND_SECONDS)
        assert view.seconds_left() == 0
        assert view.is_open() is False

    async def test_settling_pays_the_drawn_winner_the_pot_less_the_cut(self, db):
        state = await open_round(db, (ALICE, 400), (BOB, 600))
        # Ticket 500 falls inside Bob's 400-999 range.
        view = make_view(db, state, rng=FixedTicket(500))

        await view.settle()

        assert view.winner_id == BOB
        assert view.paid == jackpot.payout(1_000)
        assert (await db.get_account(BOB)).wallet == jackpot.payout(1_000)
        assert (await db.get_account(ALICE)).wallet == 0

    async def test_settling_twice_pays_once(self, db):
        state = await open_round(db, (ALICE, 400), (BOB, 600))
        view = make_view(db, state, rng=FixedTicket(500))

        await view.settle()
        await view.settle()

        assert (await db.get_account(BOB)).wallet == jackpot.payout(1_000)

    async def test_settling_closes_the_round_and_opens_the_next(self, db):
        view = make_view(db, await open_round(db, (ALICE, 400), (BOB, 600)))

        await view.settle()

        state = await db.jackpot_state()
        assert (state.round_number, state.entrants) == (2, 0)
        assert view.is_finished() is True

    async def test_a_round_nobody_joined_is_refunded_in_full(self, db):
        view = make_view(db, await open_round(db, (ALICE, ANTE)))

        await view.settle()

        assert view.refunded is True
        assert view.winner_id is None
        # Refunded in full: no cut is taken for a game that had no opponent.
        assert (await db.get_account(ALICE)).wallet == ANTE

    async def test_an_ante_landing_as_the_round_closes_is_still_in_the_pot(self, db):
        state = await open_round(db, (ALICE, 400))
        view = make_view(db, state, rng=FixedTicket(0))
        # Entered straight into the database, so the view has not seen it yet.
        await open_round(db, (BOB, 600))

        await view.settle()

        assert view.state.pot == 1_000
        assert view.paid == jackpot.payout(1_000)

    async def test_the_house_cut_feeds_the_lottery_pot(self, db):
        state = await open_round(db, (ALICE, 10_000), (BOB, 10_000))
        view = make_view(db, state, rake=0.25)

        await view.settle()

        cut = jackpot.house_cut(20_000)
        assert (await db.lottery_state()).pot == int(cut * 0.25)

    async def test_the_creator_tax_comes_out_of_the_same_cut(self, db):
        state = await open_round(db, (ALICE, 10_000), (BOB, 10_000))
        view = make_view(db, state, rake=0.25, creator_tax_rate=0.05, creator_tax_user_id=CAROL)

        await view.settle()

        cut = jackpot.house_cut(20_000)
        assert (await db.get_account(CAROL)).bank == 1_000 + int(cut * 0.05)

    async def test_a_refunded_round_rakes_nothing(self, db):
        view = make_view(db, await open_round(db, (ALICE, 10_000)), rake=0.25)

        await view.settle()

        assert (await db.lottery_state()).pot == 0

    async def test_a_round_only_ever_shrinks_the_money_supply(self, db):
        state = await open_round(db, (ALICE, 3_000), (BOB, 5_000), (CAROL, 2_000))
        view = make_view(db, state)

        await view.settle()

        held = 0
        for user in (ALICE, BOB, CAROL):
            held += (await db.get_account(user)).wallet
        assert held == jackpot.payout(10_000)
        assert held < 10_000

    async def test_the_timeout_still_draws_the_round(self, db):
        state = await open_round(db, (ALICE, 400), (BOB, 600))
        view = make_view(db, state, rng=FixedTicket(500))

        await view.on_timeout()

        assert view.winner_id == BOB
        assert (await db.jackpot_state()).entrants == 0

    async def test_a_press_spends_the_shared_action_budget(self, db):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        state = await open_round(db, (ALICE, ANTE))
        view = make_view(db, state, limiter=limiter)
        await db.add_wallet(BOB, 5 * ANTE)

        first = FakeInteraction(user=FakeUser(id=BOB))
        await JackpotView.join(view, first, None)
        assert view.state.entrants == 2

        second = FakeInteraction(user=FakeUser(id=BOB))
        await JackpotView.join(view, second, None)
        assert second.response.ephemeral, "a press past the budget was not refused"
        assert view.state.entrants == 2

    async def test_a_press_joins_the_round(self, db):
        state = await open_round(db, (ALICE, ANTE))
        view = make_view(db, state)
        await db.add_wallet(BOB, ANTE)

        interaction = FakeInteraction(user=FakeUser(id=BOB))
        await JackpotView.join(view, interaction, None)

        assert view.state.entrants == 2
        assert (await db.get_account(BOB)).wallet == 0
        assert interaction.response.edited


class TestCommand:
    async def test_the_command_opens_a_round(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)

        await cog.jackpot_command.callback(cog, ctx, ANTE)

        try:
            assert ctx.views, "no view was posted"
            assert (await db.get_account(ALICE)).wallet == 4_000
            assert (await db.jackpot_state()).pot == ANTE
        finally:
            cog._live_jackpot.cancel()

    async def test_a_second_command_joins_the_open_round(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)

        await cog.jackpot_command.callback(cog, ctx, ANTE)
        bob = FakeContext(author=FakeUser(id=BOB))
        await cog.jackpot_command.callback(cog, bob, 2_000)

        try:
            assert len(ctx.views) == 1, "a second round was opened"
            state = await db.jackpot_state()
            assert state.entrants == 2
            assert state.pot == 3_000
            assert "3,000" in bob.text
        finally:
            cog._live_jackpot.cancel()

    async def test_anteing_twice_is_refused_without_charging(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)

        await cog.jackpot_command.callback(cog, ctx, ANTE)
        await cog.jackpot_command.callback(cog, ctx, ANTE)

        try:
            assert "already in this round" in ctx.text
            assert (await db.get_account(ALICE)).wallet == 4_000
        finally:
            cog._live_jackpot.cancel()

    async def test_an_ante_over_the_table_limit_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)

        with pytest.raises(BetTooLargeError):
            await cog.jackpot_command.callback(cog, ctx, settings.max_bet + 1)

        assert (await db.get_account(ALICE)).wallet == 10_000_000
        assert (await db.jackpot_state()).entrants == 0

    async def test_the_join_button_cannot_stake_past_the_table_limit(self, db, settings, ctx):
        # The button antes what the opener did, and that has already been
        # checked, so there is no second path around the limit.
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, settings.max_bet)

        await cog.jackpot_command.callback(cog, ctx, settings.max_bet)

        try:
            assert cog._live_jackpot.ante <= settings.max_bet
        finally:
            cog._live_jackpot.cancel()

    async def test_an_orphaned_round_is_refunded_before_a_new_one_opens(self, db, settings, ctx):
        # A round left in the database with no view is one a restart, or a
        # cancelled loop, left behind.
        await open_round(db, (BOB, 2_000))
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)

        await cog.jackpot_command.callback(cog, ctx, ANTE)

        try:
            assert (await db.get_account(BOB)).wallet == 2_000
            state = await db.jackpot_state()
            assert state.entrants == 1
            assert state.pot == ANTE
        finally:
            cog._live_jackpot.cancel()

    async def test_a_finished_round_does_not_block_the_next_one(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000)

        await cog.jackpot_command.callback(cog, ctx, ANTE)
        cog._live_jackpot.cancel()
        await cog._live_jackpot.settle()

        await cog.jackpot_command.callback(cog, ctx, ANTE)

        try:
            assert len(ctx.views) == 2
            assert (await db.jackpot_state()).round_number == 2
        finally:
            cog._live_jackpot.cancel()

    async def test_startup_refunds_a_round_a_restart_interrupted(self, db, settings):
        await open_round(db, (ALICE, 2_000), (BOB, 3_000))
        cog = Gambling(FakeBot(db, settings))

        await cog.cog_load()

        assert (await db.get_account(ALICE)).wallet == 2_000
        assert (await db.get_account(BOB)).wallet == 3_000
        assert (await db.jackpot_state()).entrants == 0

    async def test_unloading_leaves_the_antes_for_the_next_startup(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await cog.jackpot_command.callback(cog, ctx, ANTE)

        await cog.cog_unload()

        assert cog._live_jackpot is None
        assert (await db.jackpot_state()).pot == ANTE
        await cog.cog_load()
        assert (await db.get_account(ALICE)).wallet == 5_000
