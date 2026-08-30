"""Tests for the lottery: its storage, its rake, and its draw."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from flyconomy.cogs.gambling import Gambling
from flyconomy.cogs.lottery import Lottery
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import InsufficientFundsError
from tests.conftest import ALICE, BOB, CAROL, make_v1_database
from tests.test_cog_behavior import FakeBot, FakeContext, FakeUser

PRICE = 10_000


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


def make_cog(db: Database, settings: Settings) -> Lottery:
    """Build the lottery cog without starting its background timer."""
    cog = Lottery.__new__(Lottery)
    from flyconomy.cogs.base import BaseCog

    BaseCog.__init__(cog, FakeBot(db, settings))
    return cog


class TestMigration:
    async def test_a_version_2_database_gains_the_lottery(self, tmp_path):
        # The bank table is untouched by migration 3.
        path = tmp_path / "bot.db"
        make_v1_database(path, [(500, 9_000, 2, 3, ALICE)])

        database = await Database.connect(path)
        try:
            account = await database.get_account(ALICE)
            state = await database.lottery_state()
        finally:
            await database.close()

        assert (account.wallet, account.bank, account.crypto, account.miner) == (500, 9_000, 2, 3)
        assert (state.pot, state.draw, state.entrants) == (0, 1, 0)

    async def test_the_pot_row_is_a_singleton(self, db, tmp_path):
        await db.add_to_pot(1)
        connection = sqlite3.connect(tmp_path / "bot.db")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO lottery (id, pot, draw) VALUES (2, 0, 1)")
            count = connection.execute("SELECT COUNT(*) FROM lottery").fetchone()[0]
        finally:
            connection.close()
        assert count == 1

    async def test_migrating_twice_keeps_the_pot(self, db_path):
        first = await Database.connect(db_path)
        await first.add_to_pot(5_000)
        await first.close()

        second = await Database.connect(db_path)
        try:
            assert (await second.lottery_state()).pot == 5_000
        finally:
            await second.close()


class TestPot:
    async def test_a_new_pot_is_empty(self, db):
        assert (await db.lottery_state()).pot == 0

    async def test_the_house_take_adds_to_the_pot(self, db):
        assert await db.add_to_pot(2_500) == 2_500
        assert await db.add_to_pot(1_500) == 4_000

    async def test_a_player_win_does_not_take_back_from_the_pot(self, db):
        await db.add_to_pot(5_000)
        assert await db.add_to_pot(-2_000) == 5_000

    async def test_a_negative_amount_alone_leaves_the_pot_at_zero(self, db):
        assert await db.add_to_pot(-10_000_000) == 0

    async def test_the_pot_is_not_a_member_balance(self, db):
        # Money in the pot is out of circulation until it is won.
        await db.add_to_pot(50_000)
        assert (await db.get_account(ALICE)).net_worth == 1_000


class TestEntry:
    async def test_entering_charges_the_bank_and_fills_the_pot(self, db):
        await db.add_bank(ALICE, 50_000)
        before = (await db.get_account(ALICE)).bank

        assert await db.enter_lottery(ALICE, PRICE) is True

        assert (await db.get_account(ALICE)).bank == before - PRICE
        assert (await db.lottery_state()).pot == PRICE

    async def test_entry_money_is_moved_not_destroyed(self, db):
        await db.add_bank(ALICE, 50_000)
        before = (await db.get_account(ALICE)).net_worth

        await db.enter_lottery(ALICE, PRICE)

        state = await db.lottery_state()
        assert (await db.get_account(ALICE)).net_worth + state.pot == before

    async def test_a_member_may_only_enter_once(self, db):
        await db.add_bank(ALICE, 100_000)

        assert await db.enter_lottery(ALICE, PRICE) is True
        assert await db.enter_lottery(ALICE, PRICE) is False

        state = await db.lottery_state()
        assert state.entrants == 1
        assert state.pot == PRICE

    async def test_a_rejected_second_entry_costs_nothing(self, db):
        await db.add_bank(ALICE, 100_000)
        await db.enter_lottery(ALICE, PRICE)
        after_first = (await db.get_account(ALICE)).bank

        await db.enter_lottery(ALICE, PRICE)

        assert (await db.get_account(ALICE)).bank == after_first

    async def test_entering_without_the_money_is_refused(self, db):
        with pytest.raises(InsufficientFundsError):
            await db.enter_lottery(ALICE, 10_000_000)

        state = await db.lottery_state()
        assert state.entrants == 0
        assert state.pot == 0

    async def test_entrants_are_listed(self, db):
        for user in (ALICE, BOB, CAROL):
            await db.add_bank(user, 50_000)
            await db.enter_lottery(user, PRICE)

        assert await db.lottery_entrants() == sorted([ALICE, BOB, CAROL])

    async def test_has_entered_reports_membership(self, db):
        await db.add_bank(ALICE, 50_000)
        assert await db.has_entered(ALICE) is False
        await db.enter_lottery(ALICE, PRICE)
        assert await db.has_entered(ALICE) is True


class TestDraw:
    async def test_the_winner_is_paid_the_whole_pot(self, db):
        await db.add_bank(ALICE, 50_000)
        await db.enter_lottery(ALICE, PRICE)
        await db.add_to_pot(90_000)
        before = (await db.get_account(ALICE)).bank

        won = await db.award_lottery(ALICE)

        assert won == 100_000
        assert (await db.get_account(ALICE)).bank == before + 100_000

    async def test_a_draw_opens_the_next_one(self, db):
        await db.add_bank(ALICE, 50_000)
        await db.enter_lottery(ALICE, PRICE)

        await db.award_lottery(ALICE)

        state = await db.lottery_state()
        assert state.draw == 2
        assert state.pot == 0
        assert state.entrants == 0

    async def test_a_member_may_enter_the_next_draw(self, db):
        await db.add_bank(ALICE, 100_000)
        await db.enter_lottery(ALICE, PRICE)
        await db.award_lottery(ALICE)

        assert await db.enter_lottery(ALICE, PRICE) is True

    async def test_rolling_over_keeps_the_pot(self, db):
        await db.add_to_pot(75_000)

        carried = await db.roll_over_lottery()

        state = await db.lottery_state()
        assert carried == 75_000
        assert state.pot == 75_000
        assert state.draw == 2

    async def test_rolling_over_clears_stale_entries(self, db):
        await db.add_bank(ALICE, 50_000)
        await db.enter_lottery(ALICE, PRICE)

        await db.roll_over_lottery()

        assert (await db.lottery_state()).entrants == 0

    async def test_a_draw_moves_money_without_creating_it(self, db):
        for user in (ALICE, BOB):
            await db.add_bank(user, 50_000)
            await db.enter_lottery(user, PRICE)

        supply = 0
        for user in (ALICE, BOB):
            supply += (await db.get_account(user)).net_worth
        pot = (await db.lottery_state()).pot

        await db.award_lottery(ALICE)

        after = 0
        for user in (ALICE, BOB):
            after += (await db.get_account(user)).net_worth
        # The pot came back into circulation; nothing was minted.
        assert after == supply + pot
        assert (await db.lottery_state()).pot == 0


class TestRake:
    """The pot is fed by the house's take, but a player win never removes
    money from it. A loss always adds its rake share; a win adds nothing."""

    async def test_a_loss_feeds_the_pot(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 100_000)

        await cog._settle(ctx, 1_000, 0)

        assert (await db.lottery_state()).pot == int(1_000 * settings.lottery_rake)

    async def test_a_win_leaves_the_pot_alone(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_to_pot(100_000)
        before = (await db.lottery_state()).pot

        await cog._settle(ctx, 1_000, 2)

        assert (await db.lottery_state()).pot == before

    async def test_churning_a_fair_game_still_feeds_the_pot_from_its_losses(
        self, db, settings, ctx
    ):
        # One win and one loss at the same stake is a whole coinflip cycle.
        # Only the loss half contributes now, so the pot grows by that share
        # every cycle instead of netting to zero.
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)
        await db.add_to_pot(1_000_000)
        before = (await db.lottery_state()).pot

        for _ in range(500):
            await cog._settle(ctx, 1_000, 0)
            await cog._settle(ctx, 1_000, 2)

        expected_gain = 500 * int(1_000 * settings.lottery_rake)
        assert (await db.lottery_state()).pot == before + expected_gain

    async def test_an_edge_game_feeds_the_pot_over_time(self, db, settings, ctx):
        # Slots returns 207 stakes per 216 spins, so the house keeps 9.
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 100_000_000)

        for _ in range(180):
            await cog._settle(ctx, 1_000, 0)
        for _ in range(36):
            await cog._settle(ctx, 1_000, 2)

        assert (await db.lottery_state()).pot > 0

    async def test_a_zero_rake_leaves_the_pot_alone(self, db, ctx):
        no_rake = Settings(discord_token="placeholder", lottery_rake=0.0)
        cog = Gambling(FakeBot(db, no_rake))
        await db.add_wallet(ALICE, 100_000)

        for _ in range(50):
            await cog._settle(ctx, 1_000, 0)

        assert (await db.lottery_state()).pot == 0

    async def test_the_rake_keeps_most_of_the_edge_as_a_sink(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)

        for _ in range(100):
            await cog._settle(ctx, 1_000, 0)

        house_take = 100 * 1_000
        potted = (await db.lottery_state()).pot
        assert potted < house_take, "the pot must not absorb the whole edge"
        assert potted == pytest.approx(house_take * settings.lottery_rake, rel=0.01)


class TestCreatorTax:
    """The creator tax is carved out of what the lottery rake leaves for
    destruction, so it never changes the pot's share or the total taken."""

    async def test_disabled_by_default_even_though_the_rate_is_nonzero(self, db, settings, ctx):
        assert settings.creator_tax_user_id is None
        assert settings.creator_tax_rate > 0
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 100_000)

        await cog._settle(ctx, 1_000, 0)

        # With no wallet configured, the loss behaves exactly like the plain
        # lottery rake: only the pot is fed.
        assert (await db.lottery_state()).pot == int(1_000 * settings.lottery_rake)

    async def test_a_loss_pays_the_creator_without_shrinking_the_pot(self, db, ctx):
        creator = 999
        taxed = Settings(discord_token="placeholder", creator_tax_user_id=creator)
        cog = Gambling(FakeBot(db, taxed))
        await db.add_wallet(ALICE, 100_000)

        await cog._settle(ctx, 1_000, 0)

        assert (await db.lottery_state()).pot == int(1_000 * taxed.lottery_rake)
        assert (await db.get_account(creator)).wallet == int(1_000 * taxed.creator_tax_rate)

    async def test_a_win_pays_the_creator_nothing(self, db, ctx):
        creator = 999
        taxed = Settings(discord_token="placeholder", creator_tax_user_id=creator)
        cog = Gambling(FakeBot(db, taxed))
        await db.add_wallet(ALICE, 100_000)

        await cog._settle(ctx, 1_000, 2)

        assert await db.find_account(creator) is None

    async def test_a_zero_tax_rate_pays_the_creator_nothing(self, db, ctx):
        creator = 999
        untaxed = Settings(
            discord_token="placeholder", creator_tax_user_id=creator, creator_tax_rate=0.0
        )
        cog = Gambling(FakeBot(db, untaxed))
        await db.add_wallet(ALICE, 100_000)

        await cog._settle(ctx, 1_000, 0)

        assert await db.find_account(creator) is None


class TestCommands:
    async def test_info_reports_the_pot(self, db, settings, ctx):
        cog = make_cog(db, settings)
        await db.add_to_pot(42_000)

        await cog.lottery.callback(cog, ctx)

        values = [f.value for f in ctx.embeds[0].fields]
        assert "$42,000" in values

    async def test_entering_confirms_the_draw(self, db, settings, ctx):
        cog = make_cog(db, settings)
        await db.add_bank(ALICE, 100_000)

        await cog.lottery_enter.callback(cog, ctx)

        assert "draw #1" in ctx.last
        assert (await db.lottery_state()).entrants == 1

    async def test_a_second_entry_is_explained(self, db, settings, ctx):
        cog = make_cog(db, settings)
        await db.add_bank(ALICE, 100_000)
        await cog.lottery_enter.callback(cog, ctx)

        await cog.lottery_enter.callback(cog, ctx)

        assert "already in this draw" in ctx.last

    async def test_entering_without_funds_is_refused(self, db, settings, ctx):
        cog = make_cog(db, settings)
        with pytest.raises(InsufficientFundsError):
            await cog.lottery_enter.callback(cog, ctx)

    async def test_an_empty_entrant_list_says_so(self, db, settings, ctx):
        cog = make_cog(db, settings)
        await cog.lottery_entrants.callback(cog, ctx)
        assert "Nobody has entered" in ctx.last

    async def test_entrants_are_mentioned(self, db, settings, ctx):
        cog = make_cog(db, settings)
        await db.add_bank(ALICE, 100_000)
        await cog.lottery_enter.callback(cog, ctx)

        await cog.lottery_entrants.callback(cog, ctx)

        assert f"<@{ALICE}>" in ctx.last


class TestRunDraw:
    async def test_a_draw_picks_an_entrant_and_pays_them(self, db, settings):
        cog = make_cog(db, settings)
        for user in (ALICE, BOB):
            await db.add_bank(user, 100_000)
            await db.enter_lottery(user, PRICE)
        await db.add_to_pot(80_000)

        winner, amount = await cog.run_draw()

        assert winner in (ALICE, BOB)
        assert amount == 100_000
        assert (await db.get_account(winner)).bank >= 100_000

    async def test_an_empty_draw_rolls_over(self, db, settings):
        cog = make_cog(db, settings)
        await db.add_to_pot(60_000)

        winner, carried = await cog.run_draw()

        assert winner is None
        assert carried == 60_000
        assert (await db.lottery_state()).pot == 60_000

    async def test_every_entrant_is_equally_likely(self, db, settings):
        # The fairness property the design exists for.
        cog = make_cog(db, settings)
        cog.rng.seed(4)
        wins = dict.fromkeys((ALICE, BOB, CAROL), 0)

        for _ in range(600):
            for user in wins:
                await db.add_bank(user, PRICE)
                await db.enter_lottery(user, PRICE)
            winner, _ = await cog.run_draw()
            assert winner is not None
            wins[winner] += 1

        for count in wins.values():
            assert 150 < count < 250, f"uneven draw: {wins}"

    async def test_playing_more_does_not_improve_your_odds(self, db, settings, ctx):
        # A grinder feeds the pot but buys no extra claim on it.
        gambling = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)
        for _ in range(500):
            await gambling._settle(ctx, 1_000, 0)

        for user in (ALICE, BOB):
            await db.add_bank(user, 100_000)
            await db.enter_lottery(user, PRICE)

        assert len(await db.lottery_entrants()) == 2
