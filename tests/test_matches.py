"""Tests for the machinery every head-to-head game shares.

``MatchChallengeView`` posts the offer and takes both stakes, and the ``escrow``
table holds them while the match is played. Neither knows which game it is
holding money for, so these are tested once, here, with tic-tac-toe standing in
as the concrete match. What that game does with its own board is tested in
tests/test_tictactoe.py.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from flyconomy import tictactoe
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import InsufficientFundsError
from flyconomy.ratelimit import SlidingWindowLimiter
from flyconomy.views import MatchChallengeView, MatchView, TicTacToeView
from tests.conftest import ALICE, BOB, CAROL
from tests.test_cog_behavior import FakeUser
from tests.test_views import FakeInteraction

BET = 1_000
POT = BET * 2
GAME = "tictactoe"


def alice() -> FakeUser:
    return FakeUser(id=ALICE, display_name="Alice")


def bob() -> FakeUser:
    return FakeUser(id=BOB, display_name="Bob")


def carol() -> FakeUser:
    return FakeUser(id=CAROL, display_name="Carol")


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


def build_match(db: Database, bet: int = BET) -> Callable[..., MatchView]:
    """Return the factory a challenge uses to turn itself into a match."""

    def build(hold_id, players):
        return TicTacToeView(
            db=db,
            game=tictactoe.Game.new(),
            players=players,
            hold_id=hold_id,
            bet=bet,
            timezone="UTC",
        )

    return build


def make_challenge(
    db: Database,
    *,
    bet: int = BET,
    challenged: FakeUser | None = None,
    limiter: SlidingWindowLimiter | None = None,
    seed: int = 7,
) -> MatchChallengeView:
    """Build a challenge from Alice, aimed at Bob unless told otherwise."""
    return MatchChallengeView(
        db=db,
        rng=random.Random(seed),
        game_name=GAME,
        title="Tic-tac-toe",
        payout=tictactoe.payout,
        build_match=build_match(db, bet),
        challenger=alice(),
        challenged=challenged if challenged is not None else bob(),
        bet=bet,
        timezone="UTC",
        timeout=tictactoe.CHALLENGE_TIMEOUT_SECONDS,
        limiter=limiter,
    )


def make_open_challenge(
    db: Database, *, bet: int = BET, limiter: SlidingWindowLimiter | None = None, seed: int = 7
) -> MatchChallengeView:
    """Build a challenge from Alice that anyone may take."""
    return MatchChallengeView(
        db=db,
        rng=random.Random(seed),
        game_name=GAME,
        title="Tic-tac-toe",
        payout=tictactoe.payout,
        build_match=build_match(db, bet),
        challenger=alice(),
        challenged=None,
        bet=bet,
        timezone="UTC",
        timeout=tictactoe.CHALLENGE_TIMEOUT_SECONDS,
        limiter=limiter,
    )


class TestEscrow:
    async def test_opening_a_hold_takes_both_stakes(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)

        hold = await db.open_escrow(GAME, ALICE, BOB, BET)

        assert hold.pot == POT
        assert (await db.get_account(ALICE)).wallet == 4_000
        assert (await db.get_account(BOB)).wallet == 4_000

    async def test_a_hold_neither_can_cover_takes_nothing(self, db):
        await db.add_wallet(ALICE, 5_000)

        with pytest.raises(InsufficientFundsError):
            await db.open_escrow(GAME, ALICE, BOB, BET)

        # Alice's stake is rolled back with Bob's, so a match that cannot start
        # leaves both players exactly as they were.
        assert (await db.get_account(ALICE)).wallet == 5_000
        assert (await db.get_account(BOB)).wallet == 0

    async def test_a_stake_of_nothing_is_refused(self, db):
        with pytest.raises(ValueError, match="positive"):
            await db.open_escrow(GAME, ALICE, BOB, 0)

    async def test_a_member_cannot_play_themselves(self, db):
        with pytest.raises(ValueError, match="two different players"):
            await db.open_escrow(GAME, ALICE, ALICE, BET)

    async def test_settling_pays_the_winner_and_releases_the_hold(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow(GAME, ALICE, BOB, BET)

        paid = await db.settle_escrow(hold.hold_id, winner_id=ALICE, cut=100)

        assert paid == POT - 100
        assert (await db.get_account(ALICE)).wallet == POT - 100
        assert await db.settle_escrow(hold.hold_id, winner_id=ALICE, cut=100) == 0

    async def test_settling_for_someone_who_did_not_play_is_refused(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow(GAME, ALICE, BOB, BET)

        with pytest.raises(ValueError, match="did not play"):
            await db.settle_escrow(hold.hold_id, winner_id=CAROL, cut=0)

    async def test_a_cut_larger_than_the_pot_is_refused(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow(GAME, ALICE, BOB, BET)

        with pytest.raises(ValueError, match="does not fit"):
            await db.settle_escrow(hold.hold_id, winner_id=ALICE, cut=POT + 1)

    async def test_refunding_hands_both_stakes_back(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow(GAME, ALICE, BOB, BET)

        assert await db.refund_escrow(hold.hold_id) == BET

        assert (await db.get_account(ALICE)).wallet == BET
        assert (await db.get_account(BOB)).wallet == BET
        assert await db.refund_escrow(hold.hold_id) == 0

    async def test_startup_refunds_every_stake_still_held(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET + 500)
        await db.add_wallet(CAROL, 500)
        await db.open_escrow(GAME, ALICE, BOB, BET)
        await db.open_escrow(GAME, BOB, CAROL, 500)

        holds = await db.refund_all_escrow()

        assert len(holds) == 2
        assert (await db.get_account(ALICE)).wallet == BET
        assert (await db.get_account(BOB)).wallet == BET + 500
        assert (await db.get_account(CAROL)).wallet == 500
        assert await db.refund_all_escrow() == []

    async def test_a_settled_hold_id_is_never_handed_out_again(self, db):
        # A stale view holding a released id must not be able to settle a
        # later match by accident.
        await db.add_wallet(ALICE, 10_000)
        await db.add_wallet(BOB, 10_000)
        first = await db.open_escrow(GAME, ALICE, BOB, BET)
        await db.settle_escrow(first.hold_id, winner_id=ALICE, cut=0)

        second = await db.open_escrow(GAME, ALICE, BOB, BET)

        assert second.hold_id != first.hold_id

    async def test_the_hold_records_which_game_it_belongs_to(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        await db.open_escrow(GAME, ALICE, BOB, BET)

        assert [hold.game for hold in await db.refund_all_escrow()] == [GAME]

    async def test_purging_a_player_voids_the_match_and_pays_the_opponent_back(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow(GAME, ALICE, BOB, BET)

        result = await db.purge_user(ALICE)

        assert result.escrow_holds == 1
        assert result.found is True
        assert (await db.get_account(BOB)).wallet == BET
        assert await db.settle_escrow(hold.hold_id, winner_id=BOB, cut=0) == 0


class TestChallenge:
    async def test_accepting_takes_both_stakes_and_starts_the_match(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db)

        assert await view.apply_accept(bob()) is None

        assert view.match is not None
        assert (await db.get_account(ALICE)).wallet == 4_000
        assert (await db.get_account(BOB)).wallet == 4_000

    async def test_the_first_move_is_drawn_rather_than_given_to_the_challenger(self, db):
        # Moving first is a real advantage, so it is not handed to whoever
        # typed the command.
        seats = set()
        for seed in range(12):
            await db.add_wallet(ALICE, BET)
            await db.add_wallet(BOB, BET)
            view = make_challenge(db, seed=seed)
            await view.apply_accept(bob())
            assert view.match is not None
            seats.add(view.match.players[0].id)
        assert seats == {ALICE, BOB}

    async def test_a_declined_challenge_stakes_nothing(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db)

        assert view.apply_decline(bob()) is None

        assert view.match is None
        assert (await db.get_account(ALICE)).wallet == 5_000
        assert (await db.get_account(BOB)).wallet == 5_000
        assert await view.apply_accept(bob()) is not None

    async def test_a_lapsed_challenge_stakes_nothing(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db)

        await view.on_timeout()

        assert (await db.get_account(ALICE)).wallet == 5_000
        assert (await db.get_account(BOB)).wallet == 5_000

    async def test_a_challenge_nobody_can_cover_names_the_short_player(self, db):
        await db.add_wallet(ALICE, 5_000)
        view = make_challenge(db)

        problem = await view.apply_accept(bob())

        assert problem is not None
        assert "Bob" in problem
        assert (await db.get_account(ALICE)).wallet == 5_000

    async def test_a_challenger_cannot_accept_their_own_challenge(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db)

        interaction = FakeInteraction(user=alice())
        await MatchChallengeView.accept(view, interaction, None)

        assert interaction.response.ephemeral
        assert view.match is None

    async def test_a_named_challenge_refuses_anybody_else(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(CAROL, 5_000)
        view = make_challenge(db)

        problem = await view.apply_accept(carol())

        assert problem is not None
        assert "for Bob" in problem
        assert view.match is None
        assert (await db.get_account(CAROL)).wallet == 5_000

    async def test_the_lapse_timer_is_a_minute(self, db):
        assert tictactoe.CHALLENGE_TIMEOUT_SECONDS == 60
        assert make_challenge(db).timeout == 60

    async def test_the_offer_shows_what_the_winner_takes(self, db):
        embed = make_challenge(db).embed()

        assert embed.description is not None
        assert f"{tictactoe.payout(POT):,}" in embed.description

    async def test_a_withdrawn_challenge_says_so(self, db):
        view = make_challenge(db)

        assert view.apply_decline(alice()) is None

        embed = view.embed()
        assert embed.description is not None
        assert "withdrawn" in embed.description

    async def test_either_side_can_close_a_named_challenge(self, db):
        assert make_challenge(db).apply_decline(alice()) is None
        assert make_challenge(db).apply_decline(bob()) is None

    async def test_an_outsider_cannot_close_a_challenge(self, db):
        view = make_challenge(db)

        problem = view.apply_decline(carol())

        assert problem is not None
        assert view.declined_by is None

    async def test_an_outsider_cannot_answer_a_named_challenge(self, db):
        view = make_challenge(db)

        interaction = FakeInteraction(user=carol())
        assert await view.interaction_check(interaction) is False
        assert interaction.response.ephemeral

    async def test_accepting_spends_the_shared_action_budget(self, db):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        limiter.acquire(BOB)
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db, limiter=limiter)

        interaction = FakeInteraction(user=bob())
        await MatchChallengeView.accept(view, interaction, None)

        assert interaction.response.ephemeral, "a press past the budget was not refused"
        assert view.match is None
        assert (await db.get_account(BOB)).wallet == 5_000


class TestOpenChallenge:
    async def test_anyone_can_take_an_open_challenge(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(CAROL, 5_000)
        view = make_open_challenge(db)

        assert view.is_open is True
        assert await view.apply_accept(carol()) is None

        assert view.accepter is not None
        assert view.accepter.id == CAROL
        assert view.match is not None
        assert {player.id for player in view.match.players} == {ALICE, CAROL}
        assert (await db.get_account(ALICE)).wallet == 4_000
        assert (await db.get_account(CAROL)).wallet == 4_000

    async def test_the_challenger_cannot_take_their_own_open_challenge(self, db):
        await db.add_wallet(ALICE, 5_000)
        view = make_open_challenge(db)

        problem = await view.apply_accept(alice())

        assert problem is not None
        assert "your own challenge" in problem
        assert view.match is None
        assert (await db.get_account(ALICE)).wallet == 5_000

    async def test_only_one_of_two_simultaneous_takers_gets_the_match(self, db):
        # Two members pressing at once must not stake the challenger twice for
        # one seat, which is what the accept lock is there for.
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        await db.add_wallet(CAROL, 5_000)
        view = make_open_challenge(db)

        results = await asyncio.gather(view.apply_accept(bob()), view.apply_accept(carol()))

        assert results.count(None) == 1, "both takers got the match"
        assert (await db.get_account(ALICE)).wallet == 4_000
        taken = {BOB, CAROL} - {
            user for user in (BOB, CAROL) if (await db.get_account(user)).wallet == 5_000
        }
        assert len(taken) == 1

    async def test_only_the_challenger_can_withdraw_an_open_challenge(self, db):
        view = make_open_challenge(db)

        assert view.apply_decline(carol()) is not None
        assert view.declined_by is None

        assert view.apply_decline(alice()) is None
        assert view.declined_by is not None

    async def test_an_open_challenge_lets_everyone_press(self, db):
        view = make_open_challenge(db)

        assert await view.interaction_check(FakeInteraction(user=carol())) is True

    async def test_an_open_challenge_says_anyone_can_accept(self, db):
        embed = make_open_challenge(db).embed()

        assert embed.description is not None
        assert "Anyone can accept" in embed.description

    async def test_a_lapsed_open_challenge_says_so(self, db):
        view = make_open_challenge(db)

        await view.on_timeout()

        embed = view.embed()
        assert embed.description is not None
        assert "open challenge" in embed.description
        assert "lapsed" in embed.description
