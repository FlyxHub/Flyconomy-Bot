"""Tests for Connect 4: the board, the escrow behind a match, and the views.

The board is pure, so the rules are checked by playing move sequences with no
database in sight. The match is driven through the views' ``apply_*``
coroutines against a real database, which is what lets a whole game -- stakes
taken, board played out, pot paid -- run without a gateway connection.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from flyconomy import connect4, embeds
from flyconomy.cogs.gambling import Gambling
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import BetTooLargeError, InsufficientFundsError
from flyconomy.ratelimit import SlidingWindowLimiter
from flyconomy.views import Connect4ChallengeView, Connect4View
from tests.conftest import ALICE, BOB, CAROL
from tests.test_cog_behavior import FakeBot, FakeContext, FakeUser
from tests.test_views import FakeInteraction

BET = 1_000
POT = BET * 2


def alice() -> FakeUser:
    return FakeUser(id=ALICE, display_name="Alice")


def bob() -> FakeUser:
    return FakeUser(id=BOB, display_name="Bob")


def play(*columns: int) -> connect4.Game:
    """Return a board with the given columns played in order."""
    game = connect4.Game.new()
    for column in columns:
        game.drop(column)
    return game


def full_board() -> connect4.Game:
    """Return a full board with no line of four anywhere on it.

    Built directly rather than played out: the property under test is that a
    full board with no winner reads as a draw, and the shortest honest way to
    state that is to hand it one. Every column repeats 1,1,2 with the odd
    columns mirrored, which keeps every run -- vertical, horizontal, and both
    diagonals -- to three. `test_the_drawn_board_really_has_no_line` checks
    that rather than taking it on trust.
    """
    pattern = [connect4.FIRST, connect4.FIRST, connect4.SECOND] * 2
    mirror = [connect4.SECOND, connect4.SECOND, connect4.FIRST] * 2
    columns = [list(pattern if column % 2 == 0 else mirror) for column in range(connect4.COLUMNS)]
    return connect4.Game(columns=columns)


def nearly_full_board() -> connect4.Game:
    """Return the drawn board one disc short, with that disc's owner to move."""
    game = full_board()
    missing = game.columns[6].pop()
    game.turn = missing
    return game


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
    return FakeContext(author=alice())


async def make_match(
    db: Database,
    *,
    bet: int = BET,
    game: connect4.Game | None = None,
    rake: float = 0.0,
    creator_tax_rate: float = 0.0,
    creator_tax_user_id: int | None = None,
    funded: bool = True,
) -> Connect4View:
    """Take two stakes into escrow and return a view over the resulting match."""
    if funded:
        await db.add_wallet(ALICE, bet)
        await db.add_wallet(BOB, bet)
    hold = await db.open_escrow("connect4", ALICE, BOB, bet)
    return Connect4View(
        db=db,
        game=game if game is not None else connect4.Game.new(),
        players=(alice(), bob()),
        hold_id=hold.hold_id,
        bet=bet,
        timezone="UTC",
        rake=rake,
        creator_tax_rate=creator_tax_rate,
        creator_tax_user_id=creator_tax_user_id,
    )


def make_challenge(
    db: Database,
    *,
    bet: int = BET,
    limiter: SlidingWindowLimiter | None = None,
    seed: int = 7,
) -> Connect4ChallengeView:
    """Build a challenge from Alice to Bob."""
    return Connect4ChallengeView(
        db=db,
        rng=random.Random(seed),
        challenger=alice(),
        opponent=bob(),
        bet=bet,
        timezone="UTC",
        limiter=limiter,
    )


class TestBoard:
    def test_a_new_board_is_empty_with_the_first_player_to_move(self):
        game = connect4.Game.new()
        assert game.moves == 0
        assert game.turn == connect4.FIRST
        assert game.finished is False
        assert game.open_columns == tuple(range(connect4.COLUMNS))

    def test_a_disc_lands_at_the_bottom_of_its_column(self):
        game = connect4.Game.new()
        assert game.drop(3) == 0
        assert game.cell(3, 0) == connect4.FIRST

    def test_discs_stack_on_each_other(self):
        game = play(3, 3)
        assert game.cell(3, 0) == connect4.FIRST
        assert game.cell(3, 1) == connect4.SECOND
        assert game.height(3) == 2

    def test_the_turn_alternates(self):
        game = connect4.Game.new()
        assert game.turn == connect4.FIRST
        game.drop(0)
        assert game.turn == connect4.SECOND
        game.drop(1)
        assert game.turn == connect4.FIRST

    def test_an_empty_cell_reads_as_empty(self):
        assert connect4.Game.new().cell(0, 0) == connect4.EMPTY

    def test_a_cell_off_the_board_reads_as_empty(self):
        game = connect4.Game.new()
        assert game.cell(-1, 0) == connect4.EMPTY
        assert game.cell(connect4.COLUMNS, 0) == connect4.EMPTY
        assert game.cell(0, connect4.ROWS) == connect4.EMPTY

    def test_a_full_column_takes_no_more_discs(self):
        # Dropping into one column repeatedly alternates the discs, so the
        # column fills without anybody connecting four.
        game = play(*([0] * connect4.ROWS))
        assert game.height(0) == connect4.ROWS
        assert game.can_drop(0) is False
        assert 0 not in game.open_columns
        with pytest.raises(ValueError, match="full"):
            game.drop(0)

    @pytest.mark.parametrize("column", [-1, connect4.COLUMNS])
    def test_a_column_off_the_board_is_refused(self, column):
        with pytest.raises(ValueError, match="not on the board"):
            connect4.Game.new().drop(column)

    def test_a_decided_board_takes_no_more_discs(self):
        game = play(0, 1, 0, 1, 0, 1, 0)
        assert game.winner == connect4.FIRST
        with pytest.raises(ValueError, match="already decided"):
            game.drop(4)

    def test_the_other_player_is_the_one_not_named(self):
        assert connect4.other_player(connect4.FIRST) == connect4.SECOND
        assert connect4.other_player(connect4.SECOND) == connect4.FIRST


class TestWinning:
    def test_four_in_a_row_wins(self):
        game = play(0, 0, 1, 1, 2, 2, 3)
        assert game.winner == connect4.FIRST
        assert game.winning_cells == ((0, 0), (1, 0), (2, 0), (3, 0))

    def test_four_in_a_column_wins(self):
        game = play(0, 1, 0, 1, 0, 1, 0)
        assert game.winner == connect4.FIRST
        assert game.winning_cells == ((0, 0), (0, 1), (0, 2), (0, 3))

    def test_four_on_a_rising_diagonal_wins(self):
        # A diagonal needs its lower cells propping it up, and those take a
        # move each, so the second player is the one who can complete one.
        game = play(1, 0, 2, 1, 2, 2, 3, 3, 3, 3)
        assert game.winner == connect4.SECOND
        assert game.winning_cells == ((0, 0), (1, 1), (2, 2), (3, 3))

    def test_four_on_a_falling_diagonal_wins(self):
        game = play(5, 6, 4, 5, 4, 4, 3, 3, 3, 3)
        assert game.winner == connect4.SECOND
        assert game.winning_cells == ((3, 3), (4, 2), (5, 1), (6, 0))

    def test_three_in_a_row_is_not_a_win(self):
        game = play(0, 0, 1, 1, 2, 2)
        assert game.winner is None
        assert game.finished is False

    def test_a_winning_move_does_not_pass_the_turn(self):
        # The loser is always the other player, which is what lets the view
        # name a winner without tracking who moved last.
        game = play(0, 1, 0, 1, 0, 1, 0)
        assert game.turn == game.winner == connect4.FIRST

    def test_the_drawn_board_really_has_no_line(self):
        game = full_board()
        for column in range(connect4.COLUMNS):
            for row in range(connect4.ROWS):
                player = game.cell(column, row)
                assert game._line_through(column, row, player) is None, (
                    f"({column}, {row}) is part of a line, so this is not a draw"
                )

    def test_a_full_board_with_no_line_is_a_draw(self):
        game = full_board()
        assert game.is_full is True
        assert game.winner is None
        assert game.is_draw is True
        assert game.finished is True

    def test_a_won_board_is_not_a_draw(self):
        game = play(0, 1, 0, 1, 0, 1, 0)
        assert game.is_draw is False


class TestPayout:
    def test_the_winner_takes_the_pot_less_the_cut(self):
        assert connect4.payout(POT) == POT - connect4.house_cut(POT)

    @pytest.mark.parametrize("pot", [0, 2, 19, 200, 2_000, 1_000_000])
    def test_a_match_never_pays_out_more_than_was_staked(self, pot):
        assert connect4.payout(pot) <= pot

    def test_an_even_match_loses_the_cut_on_average(self):
        # Two evenly matched players each win half the time, so each expects
        # half the payout against a whole stake.
        stake = 10_000
        expected = 0.5 * connect4.payout(stake * 2)
        assert expected < stake
        assert expected == pytest.approx(stake * (1 - connect4.HOUSE_CUT), abs=1)


class TestEscrow:
    async def test_opening_a_hold_takes_both_stakes(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)

        hold = await db.open_escrow("connect4", ALICE, BOB, BET)

        assert hold.pot == POT
        assert (await db.get_account(ALICE)).wallet == 4_000
        assert (await db.get_account(BOB)).wallet == 4_000

    async def test_a_hold_neither_can_cover_takes_nothing(self, db):
        await db.add_wallet(ALICE, 5_000)

        with pytest.raises(InsufficientFundsError):
            await db.open_escrow("connect4", ALICE, BOB, BET)

        # Alice's stake is rolled back with Bob's, so a match that cannot start
        # leaves both players exactly as they were.
        assert (await db.get_account(ALICE)).wallet == 5_000
        assert (await db.get_account(BOB)).wallet == 0

    async def test_a_stake_of_nothing_is_refused(self, db):
        with pytest.raises(ValueError, match="positive"):
            await db.open_escrow("connect4", ALICE, BOB, 0)

    async def test_a_member_cannot_play_themselves(self, db):
        with pytest.raises(ValueError, match="two different players"):
            await db.open_escrow("connect4", ALICE, ALICE, BET)

    async def test_settling_pays_the_winner_and_releases_the_hold(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow("connect4", ALICE, BOB, BET)

        paid = await db.settle_escrow(hold.hold_id, winner_id=ALICE, cut=100)

        assert paid == POT - 100
        assert (await db.get_account(ALICE)).wallet == POT - 100
        assert await db.settle_escrow(hold.hold_id, winner_id=ALICE, cut=100) == 0

    async def test_settling_for_someone_who_did_not_play_is_refused(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow("connect4", ALICE, BOB, BET)

        with pytest.raises(ValueError, match="did not play"):
            await db.settle_escrow(hold.hold_id, winner_id=CAROL, cut=0)

    async def test_a_cut_larger_than_the_pot_is_refused(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow("connect4", ALICE, BOB, BET)

        with pytest.raises(ValueError, match="does not fit"):
            await db.settle_escrow(hold.hold_id, winner_id=ALICE, cut=POT + 1)

    async def test_refunding_hands_both_stakes_back(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow("connect4", ALICE, BOB, BET)

        assert await db.refund_escrow(hold.hold_id) == BET

        assert (await db.get_account(ALICE)).wallet == BET
        assert (await db.get_account(BOB)).wallet == BET
        assert await db.refund_escrow(hold.hold_id) == 0

    async def test_startup_refunds_every_stake_still_held(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET + 500)
        await db.add_wallet(CAROL, 500)
        await db.open_escrow("connect4", ALICE, BOB, BET)
        await db.open_escrow("connect4", BOB, CAROL, 500)

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
        first = await db.open_escrow("connect4", ALICE, BOB, BET)
        await db.settle_escrow(first.hold_id, winner_id=ALICE, cut=0)

        second = await db.open_escrow("connect4", ALICE, BOB, BET)

        assert second.hold_id != first.hold_id

    async def test_purging_a_player_voids_the_match_and_pays_the_opponent_back(self, db):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        hold = await db.open_escrow("connect4", ALICE, BOB, BET)

        result = await db.purge_user(ALICE)

        assert result.escrow_holds == 1
        assert result.found is True
        assert (await db.get_account(BOB)).wallet == BET
        assert await db.settle_escrow(hold.hold_id, winner_id=BOB, cut=0) == 0


class TestChallenge:
    async def test_accepting_takes_both_stakes_and_starts_the_board(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db)

        assert await view.apply_accept() is None

        assert view.match is not None
        assert view.match.game.moves == 0
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
            await view.apply_accept()
            assert view.match is not None
            seats.add(view.match.players[0].id)
        assert seats == {ALICE, BOB}

    async def test_a_declined_challenge_stakes_nothing(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db)

        view.apply_decline(bob())

        assert view.match is None
        assert (await db.get_account(ALICE)).wallet == 5_000
        assert (await db.get_account(BOB)).wallet == 5_000
        assert await view.apply_accept() is not None

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

        problem = await view.apply_accept()

        assert problem is not None
        assert "Bob" in problem
        assert (await db.get_account(ALICE)).wallet == 5_000

    async def test_only_the_challenged_member_can_accept(self, db):
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db)

        interaction = FakeInteraction(user=alice())
        await Connect4ChallengeView.accept(view, interaction, None)

        assert interaction.response.ephemeral
        assert view.match is None

    async def test_an_outsider_cannot_answer_the_challenge(self, db):
        view = make_challenge(db)

        interaction = FakeInteraction(user=FakeUser(id=CAROL))
        assert await view.interaction_check(interaction) is False
        assert interaction.response.ephemeral

    async def test_accepting_spends_the_shared_action_budget(self, db):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        limiter.acquire(BOB)
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        view = make_challenge(db, limiter=limiter)

        interaction = FakeInteraction(user=bob())
        await Connect4ChallengeView.accept(view, interaction, None)

        assert interaction.response.ephemeral, "a press past the budget was not refused"
        assert view.match is None
        assert (await db.get_account(BOB)).wallet == 5_000


class TestMatch:
    async def test_only_the_player_to_move_may_drop(self, db):
        view = await make_match(db)
        waiting = view.opponent_of(view.to_move)

        problem = await view.apply_drop(waiting.id, 3)

        assert problem is not None
        assert "turn" in problem
        assert view.game.moves == 0

    async def test_a_full_column_is_refused(self, db):
        view = await make_match(db, game=play(*([0] * connect4.ROWS)))

        problem = await view.apply_drop(view.to_move.id, 0)

        assert problem is not None
        assert "full" in problem

    async def test_a_win_pays_the_pot_less_the_cut(self, db):
        view = await make_match(db)
        first, second = view.players
        for column in (0, 1, 0, 1, 0, 1):
            await view.apply_drop(view.to_move.id, column)

        await view.apply_drop(first.id, 0)

        assert view.winner is not None
        assert view.winner.id == first.id
        assert view.paid == connect4.payout(POT)
        assert (await db.get_account(first.id)).wallet == connect4.payout(POT)
        assert (await db.get_account(second.id)).wallet == 0

    async def test_a_draw_returns_both_stakes(self, db):
        # One disc short of the drawn board, so the last move fills it.
        view = await make_match(db, game=nearly_full_board())

        await view.apply_drop(view.to_move.id, 6)

        assert view.drawn is True
        assert view.winner is None
        assert (await db.get_account(ALICE)).wallet == BET
        assert (await db.get_account(BOB)).wallet == BET

    async def test_resigning_hands_the_pot_to_the_other_player(self, db):
        view = await make_match(db)
        quitter, winner = view.players

        assert await view.apply_resign(quitter.id) is None

        assert view.forfeited is True
        assert view.winner is not None
        assert view.winner.id == winner.id
        assert (await db.get_account(winner.id)).wallet == connect4.payout(POT)

    async def test_running_out_of_time_forfeits_for_whoever_was_to_move(self, db):
        view = await make_match(db)
        stalled = view.to_move
        winner = view.opponent_of(stalled)

        await view.on_timeout()

        assert view.forfeited is True
        assert view.winner is not None
        assert view.winner.id == winner.id
        assert (await db.get_account(winner.id)).wallet == connect4.payout(POT)

    async def test_settling_twice_pays_once(self, db):
        view = await make_match(db)
        quitter, winner = view.players

        await view.apply_resign(quitter.id)
        await view.on_timeout()
        await view.settle()

        assert (await db.get_account(winner.id)).wallet == connect4.payout(POT)

    async def test_a_finished_match_takes_no_more_moves(self, db):
        view = await make_match(db)
        await view.apply_resign(view.players[0].id)

        problem = await view.apply_drop(view.players[1].id, 0)

        assert problem is not None
        assert "already over" in problem

    async def test_a_purged_match_voids_instead_of_paying(self, db):
        view = await make_match(db)
        await db.purge_user(ALICE)

        await view.apply_resign(view.players[0].id)

        assert view.voided is True
        assert view.winner is None
        assert view.paid == 0

    async def test_the_house_cut_feeds_the_lottery_pot(self, db):
        view = await make_match(db, bet=10_000, rake=0.25)

        await view.apply_resign(view.players[0].id)

        cut = connect4.house_cut(20_000)
        assert (await db.lottery_state()).pot == int(cut * 0.25)

    async def test_the_creator_tax_comes_out_of_the_same_cut(self, db):
        view = await make_match(
            db, bet=10_000, rake=0.25, creator_tax_rate=0.05, creator_tax_user_id=CAROL
        )

        await view.apply_resign(view.players[0].id)

        cut = connect4.house_cut(20_000)
        assert (await db.get_account(CAROL)).bank == 1_000 + int(cut * 0.05)

    async def test_a_draw_rakes_nothing(self, db):
        view = await make_match(db, game=nearly_full_board(), rake=0.25)

        await view.apply_drop(view.to_move.id, 6)

        assert (await db.lottery_state()).pot == 0

    async def test_a_match_only_ever_shrinks_the_money_supply(self, db):
        view = await make_match(db)

        await view.apply_resign(view.players[0].id)

        held = 0
        for user in (ALICE, BOB):
            held += (await db.get_account(user)).wallet
        assert held == connect4.payout(POT)
        assert held < POT

    async def test_an_outsider_cannot_touch_the_board(self, db):
        view = await make_match(db)

        interaction = FakeInteraction(user=FakeUser(id=CAROL))
        assert await view.interaction_check(interaction) is False
        assert interaction.response.ephemeral

    async def test_a_player_can_press(self, db):
        view = await make_match(db)
        assert await view.interaction_check(FakeInteraction(user=view.to_move)) is True

    async def test_a_column_button_drops_a_disc_and_redraws(self, db):
        view = await make_match(db)
        button = next(child for child in view.children if getattr(child, "column", None) == 3)

        interaction = FakeInteraction(user=view.to_move)
        await button.callback(interaction)

        assert view.game.cell(3, 0) != connect4.EMPTY
        assert interaction.response.edited

    async def test_a_full_column_disables_its_button(self, db):
        view = await make_match(db)
        for _ in range(connect4.ROWS):
            await view.apply_drop(view.to_move.id, 0)

        button = next(child for child in view.children if getattr(child, "column", None) == 0)
        assert button.disabled is True

    async def test_the_board_shows_the_winning_line(self, db):
        view = await make_match(db)
        for column in (0, 1, 0, 1, 0, 1, 0):
            await view.apply_drop(view.to_move.id, column)

        board = embeds.connect4_board(view.game)
        assert "\N{LARGE RED SQUARE}" in board or "\N{LARGE YELLOW SQUARE}" in board


class TestCommand:
    async def test_the_command_posts_a_challenge_without_staking(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)

        await cog.connect4_command.callback(cog, ctx, bob(), BET)

        assert ctx.views, "no challenge was posted"
        assert (await db.get_account(ALICE)).wallet == 5_000
        assert (await db.get_account(BOB)).wallet == 5_000

    async def test_challenging_yourself_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))

        await cog.connect4_command.callback(cog, ctx, alice(), BET)

        assert "cannot play yourself" in ctx.text
        assert not ctx.views

    async def test_challenging_a_bot_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))

        await cog.connect4_command.callback(cog, ctx, FakeUser(id=BOB, bot=True), BET)

        assert "Bots do not play" in ctx.text
        assert not ctx.views

    async def test_a_stake_over_the_table_limit_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)

        with pytest.raises(BetTooLargeError):
            await cog.connect4_command.callback(cog, ctx, bob(), settings.max_bet + 1)

        assert (await db.get_account(ALICE)).wallet == 10_000_000

    async def test_startup_refunds_a_match_a_restart_interrupted(self, db, settings):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        await db.open_escrow("connect4", ALICE, BOB, BET)
        cog = Gambling(FakeBot(db, settings))

        await cog.cog_load()

        assert (await db.get_account(ALICE)).wallet == BET
        assert (await db.get_account(BOB)).wallet == BET
