"""Tests for tic-tac-toe: the board, the best-of-three match, and its buttons.

The board is pure, so the rules are checked by playing sequences of cells with
no database in sight. The match is driven through the view's ``apply_*``
coroutines against a real database, which is what lets a whole match -- stakes
taken, boards played out, pot paid -- run without a gateway connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import discord
import pytest

from flyconomy import tictactoe
from flyconomy.cogs.gambling import Gambling
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import BetTooLargeError
from flyconomy.views import MatchChallengeView, TicTacToeView
from tests.conftest import ALICE, BOB, CAROL
from tests.test_cog_behavior import FakeBot, FakeContext, FakeUser
from tests.test_views import FakeInteraction

BET = 1_000
POT = BET * 2

#: A full board with no line on it. Verified rather than assumed by
#: `test_the_drawing_sequence_really_draws`.
DRAW_SEQUENCE = (0, 1, 2, 4, 3, 5, 7, 6, 8)

#: The first player takes the top row.
WIN_SEQUENCE = (0, 3, 1, 4, 2)


def alice() -> FakeUser:
    return FakeUser(id=ALICE, display_name="Alice")


def bob() -> FakeUser:
    return FakeUser(id=BOB, display_name="Bob")


def carol() -> FakeUser:
    return FakeUser(id=CAROL, display_name="Carol")


def play(*cells: int) -> tictactoe.Game:
    """Return a board with the given cells played in order."""
    game = tictactoe.Game.new()
    for index in cells:
        game.place(index)
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
    rake: float = 0.0,
    creator_tax_rate: float = 0.0,
    creator_tax_user_id: int | None = None,
) -> TicTacToeView:
    """Take two stakes into escrow and return a view over the resulting match."""
    await db.add_wallet(ALICE, bet)
    await db.add_wallet(BOB, bet)
    hold = await db.open_escrow("tictactoe", ALICE, BOB, bet)
    return TicTacToeView(
        db=db,
        game=tictactoe.Game.new(),
        players=(alice(), bob()),
        hold_id=hold.hold_id,
        bet=bet,
        timezone="UTC",
        rake=rake,
        creator_tax_rate=creator_tax_rate,
        creator_tax_user_id=creator_tax_user_id,
    )


async def play_out(view: TicTacToeView, cells: tuple[int, ...]) -> None:
    """Play a sequence of cells, each by whoever is to move."""
    for index in cells:
        await view.apply_place(view.to_move.id, index)


def cell_buttons(view: TicTacToeView) -> list[discord.ui.Button[TicTacToeView]]:
    """Return the nine grid buttons, in cell order."""
    buttons = [child for child in view.children if getattr(child, "index", None) is not None]
    return sorted(buttons, key=lambda child: child.index)


class TestBoard:
    def test_a_new_board_is_empty_with_the_first_player_to_move(self):
        game = tictactoe.Game.new()
        assert game.moves == 0
        assert game.turn == tictactoe.FIRST
        assert game.finished is False
        assert game.open_cells == tuple(range(tictactoe.CELLS))

    def test_a_mark_lands_in_the_cell_it_was_played_in(self):
        game = play(4)
        assert game.cell(4) == tictactoe.FIRST
        assert game.cell(0) == tictactoe.EMPTY

    def test_the_turn_alternates(self):
        game = tictactoe.Game.new()
        assert game.turn == tictactoe.FIRST
        game.place(0)
        assert game.turn == tictactoe.SECOND
        game.place(1)
        assert game.turn == tictactoe.FIRST

    def test_a_taken_cell_is_refused(self):
        game = play(4)
        assert game.can_place(4) is False
        with pytest.raises(ValueError, match="already taken"):
            game.place(4)

    @pytest.mark.parametrize("index", [-1, tictactoe.CELLS])
    def test_a_cell_off_the_board_is_refused(self, index):
        with pytest.raises(ValueError, match="not on the board"):
            tictactoe.Game.new().place(index)

    def test_a_cell_off_the_board_reads_as_empty(self):
        game = tictactoe.Game.new()
        assert game.cell(-1) == tictactoe.EMPTY
        assert game.cell(tictactoe.CELLS) == tictactoe.EMPTY

    def test_a_decided_board_takes_no_more_marks(self):
        game = play(*WIN_SEQUENCE)
        with pytest.raises(ValueError, match="already decided"):
            game.place(8)

    def test_open_cells_shrink_as_the_board_fills(self):
        game = play(0, 4)
        assert game.open_cells == (1, 2, 3, 5, 6, 7, 8)

    def test_the_other_player_is_the_one_not_named(self):
        assert tictactoe.other_player(tictactoe.FIRST) == tictactoe.SECOND
        assert tictactoe.other_player(tictactoe.SECOND) == tictactoe.FIRST

    def test_there_are_nine_cells_in_three_rows(self):
        assert tictactoe.CELLS == 9
        assert tictactoe.SIZE == 3


class TestWinning:
    @pytest.mark.parametrize("line", tictactoe.LINES)
    def test_every_line_wins(self, line):
        # The first player takes the line while the second answers elsewhere.
        elsewhere = [cell for cell in range(tictactoe.CELLS) if cell not in line]
        game = tictactoe.Game.new()
        for index, cell in enumerate(line):
            game.place(cell)
            if game.finished:
                break
            game.place(elsewhere[index])

        assert game.winner == tictactoe.FIRST
        assert set(game.winning_cells) == set(line)

    def test_there_are_eight_lines(self):
        assert len(tictactoe.LINES) == 8
        assert len(set(tictactoe.LINES)) == 8

    def test_two_in_a_line_is_not_a_win(self):
        game = play(0, 4, 1)
        assert game.winner is None
        assert game.finished is False

    def test_a_winning_move_does_not_pass_the_turn(self):
        # The loser is always the other player, which is what lets the view
        # name a winner without tracking who moved last.
        game = play(*WIN_SEQUENCE)
        assert game.turn == game.winner == tictactoe.FIRST

    def test_the_drawing_sequence_really_draws(self):
        game = play(*DRAW_SEQUENCE)
        assert game.is_full is True
        assert game.winner is None
        assert game.is_draw is True
        assert game.finished is True

    def test_a_won_board_is_not_a_draw(self):
        assert play(*WIN_SEQUENCE).is_draw is False


class TestPayout:
    def test_the_winner_takes_the_pot_less_the_cut(self):
        assert tictactoe.payout(POT) == POT - tictactoe.house_cut(POT)

    @pytest.mark.parametrize("pot", [0, 2, 19, 200, 2_000, 1_000_000])
    def test_a_match_never_pays_out_more_than_was_staked(self, pot):
        assert tictactoe.payout(pot) <= pot

    def test_an_even_match_loses_the_cut_on_average(self):
        stake = 10_000
        expected = 0.5 * tictactoe.payout(stake * 2)
        assert expected < stake
        assert expected == pytest.approx(stake * (1 - tictactoe.HOUSE_CUT), abs=1)

    def test_a_match_is_an_odd_number_of_boards(self):
        # Seats swap each board, so an odd count shares the first move as
        # evenly as it can be shared.
        assert tictactoe.BOARDS_PER_MATCH % 2 == 1


class TestMatch:
    async def test_only_the_player_to_move_may_take_a_square(self, db):
        view = await make_match(db)
        waiting = view.opponent_of(view.to_move)

        problem = await view.apply_place(waiting.id, 4)

        assert problem is not None
        assert "turn" in problem
        assert view.game.moves == 0

    async def test_a_taken_square_is_refused(self, db):
        view = await make_match(db)
        await view.apply_place(view.to_move.id, 4)

        problem = await view.apply_place(view.to_move.id, 4)

        assert problem is not None
        assert "taken" in problem

    async def test_a_win_pays_the_pot_less_the_cut(self, db):
        view = await make_match(db)
        first, second = view.players

        await play_out(view, WIN_SEQUENCE)

        assert view.winner is not None
        assert view.winner.id == first.id
        assert view.paid == tictactoe.payout(POT)
        assert (await db.get_account(first.id)).wallet == tictactoe.payout(POT)
        assert (await db.get_account(second.id)).wallet == 0

    async def test_a_drawn_board_starts_the_next_one_instead_of_settling(self, db):
        view = await make_match(db)
        opened_with = view.players

        await play_out(view, DRAW_SEQUENCE)

        assert view.settled is False
        assert view.board_number == 2
        assert view.game.moves == 0
        # The seats swap, because moving first is the whole of the advantage.
        assert view.players == (opened_with[1], opened_with[0])
        # Nothing moved: the stakes are still held.
        assert (await db.get_account(ALICE)).wallet == 0
        assert (await db.get_account(BOB)).wallet == 0

    async def test_a_win_on_a_later_board_still_pays(self, db):
        view = await make_match(db)

        await play_out(view, DRAW_SEQUENCE)
        winner = view.to_move
        await play_out(view, WIN_SEQUENCE)

        assert view.board_number == 2
        assert view.winner is not None
        assert view.winner.id == winner.id
        assert (await db.get_account(winner.id)).wallet == tictactoe.payout(POT)

    async def test_a_match_of_drawn_boards_returns_both_stakes(self, db):
        view = await make_match(db)

        for _ in range(tictactoe.BOARDS_PER_MATCH):
            await play_out(view, DRAW_SEQUENCE)

        assert view.settled is True
        assert view.drawn is True
        assert view.winner is None
        assert view.board_number == tictactoe.BOARDS_PER_MATCH
        assert (await db.get_account(ALICE)).wallet == BET
        assert (await db.get_account(BOB)).wallet == BET

    async def test_a_fully_drawn_match_rakes_nothing(self, db):
        view = await make_match(db, rake=0.25)

        for _ in range(tictactoe.BOARDS_PER_MATCH):
            await play_out(view, DRAW_SEQUENCE)

        assert (await db.lottery_state()).pot == 0

    async def test_resigning_hands_the_pot_to_the_other_player(self, db):
        view = await make_match(db)
        quitter, winner = view.players

        assert await view.apply_resign(quitter.id) is None

        assert view.forfeited is True
        assert view.winner is not None
        assert view.winner.id == winner.id
        assert (await db.get_account(winner.id)).wallet == tictactoe.payout(POT)

    async def test_running_out_of_time_forfeits_for_whoever_was_to_move(self, db):
        view = await make_match(db)
        stalled = view.to_move
        winner = view.opponent_of(stalled)

        await view.on_timeout()

        assert view.forfeited is True
        assert view.winner is not None
        assert view.winner.id == winner.id
        assert (await db.get_account(winner.id)).wallet == tictactoe.payout(POT)

    async def test_settling_twice_pays_once(self, db):
        view = await make_match(db)
        quitter, winner = view.players

        await view.apply_resign(quitter.id)
        await view.on_timeout()
        await view.settle()

        assert (await db.get_account(winner.id)).wallet == tictactoe.payout(POT)

    async def test_a_finished_match_takes_no_more_moves(self, db):
        view = await make_match(db)
        await view.apply_resign(view.players[0].id)

        problem = await view.apply_place(view.players[1].id, 0)

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

        cut = tictactoe.house_cut(20_000)
        assert (await db.lottery_state()).pot == int(cut * 0.25)

    async def test_the_creator_tax_comes_out_of_the_same_cut(self, db):
        view = await make_match(
            db, bet=10_000, rake=0.25, creator_tax_rate=0.05, creator_tax_user_id=CAROL
        )

        await view.apply_resign(view.players[0].id)

        cut = tictactoe.house_cut(20_000)
        assert (await db.get_account(CAROL)).bank == 1_000 + int(cut * 0.05)

    async def test_a_match_only_ever_shrinks_the_money_supply(self, db):
        view = await make_match(db)

        await view.apply_resign(view.players[0].id)

        held = 0
        for user in (ALICE, BOB):
            held += (await db.get_account(user)).wallet
        assert held == tictactoe.payout(POT)
        assert held < POT

    async def test_an_outsider_cannot_touch_the_board(self, db):
        view = await make_match(db)

        interaction = FakeInteraction(user=carol())
        assert await view.interaction_check(interaction) is False
        assert interaction.response.ephemeral


class TestButtons:
    async def test_the_grid_is_three_rows_of_three(self, db):
        view = await make_match(db)

        rows = [button.row for button in cell_buttons(view)]

        assert len(rows) == 9
        assert rows == [0, 0, 0, 1, 1, 1, 2, 2, 2]

    async def test_no_row_holds_more_than_discord_allows(self, db):
        view = await make_match(db)

        counts: dict[int | None, int] = {}
        for child in view.children:
            counts[child.row] = counts.get(child.row, 0) + 1

        assert max(counts.values()) <= 5, "a row would wrap"

    async def test_an_empty_square_shows_nothing(self, db):
        view = await make_match(db)

        assert cell_buttons(view)[0].label == "\N{ZERO WIDTH SPACE}"

    async def test_a_played_square_shows_its_mark_and_locks(self, db):
        view = await make_match(db)

        await view.apply_place(view.to_move.id, 4)

        played = cell_buttons(view)[4]
        assert played.label == "X"
        assert played.disabled is True

    async def test_the_second_player_marks_with_a_nought(self, db):
        view = await make_match(db)
        await view.apply_place(view.to_move.id, 4)
        await view.apply_place(view.to_move.id, 0)

        assert cell_buttons(view)[0].label == "O"

    async def test_the_winning_line_turns_green(self, db):
        view = await make_match(db)

        await play_out(view, WIN_SEQUENCE)

        buttons = cell_buttons(view)
        assert all(buttons[cell].style is discord.ButtonStyle.success for cell in (0, 1, 2))
        assert buttons[3].style is not discord.ButtonStyle.success

    async def test_every_button_locks_once_the_match_is_over(self, db):
        view = await make_match(db)

        await view.apply_resign(view.players[0].id)

        assert all(child.disabled for child in view.children)

    async def test_a_press_takes_the_square_and_redraws(self, db):
        view = await make_match(db)
        button = cell_buttons(view)[4]

        interaction = FakeInteraction(user=view.to_move)
        await button.callback(interaction)

        assert view.game.cell(4) != tictactoe.EMPTY
        assert interaction.response.edited

    async def test_a_press_out_of_turn_is_refused_privately(self, db):
        view = await make_match(db)
        button = cell_buttons(view)[4]

        interaction = FakeInteraction(user=view.opponent_of(view.to_move))
        await button.callback(interaction)

        assert interaction.response.ephemeral
        assert not interaction.response.edited
        assert view.game.moves == 0


class TestCommand:
    async def test_the_command_posts_a_challenge_without_staking(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)

        await cog.tictactoe_command.callback(cog, ctx, bob(), BET)

        assert ctx.views, "no challenge was posted"
        assert (await db.get_account(ALICE)).wallet == 5_000
        assert (await db.get_account(BOB)).wallet == 5_000

    async def test_leaving_the_member_out_posts_an_open_challenge(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)

        await cog.tictactoe_command.callback(cog, ctx, None, BET)

        assert ctx.views[0].is_open is True

    async def test_accepting_starts_a_tic_tac_toe_match(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        await cog.tictactoe_command.callback(cog, ctx, bob(), BET)
        challenge: MatchChallengeView = ctx.views[0]

        assert await challenge.apply_accept(bob()) is None

        assert isinstance(challenge.match, TicTacToeView)
        assert (await db.get_account(ALICE)).wallet == 4_000
        assert (await db.get_account(BOB)).wallet == 4_000

    async def test_challenging_yourself_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))

        await cog.tictactoe_command.callback(cog, ctx, alice(), BET)

        assert "cannot play yourself" in ctx.text
        assert not ctx.views

    async def test_challenging_a_bot_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))

        await cog.tictactoe_command.callback(cog, ctx, FakeUser(id=BOB, bot=True), BET)

        assert "Bots do not play" in ctx.text
        assert not ctx.views

    async def test_a_stake_over_the_table_limit_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)

        with pytest.raises(BetTooLargeError):
            await cog.tictactoe_command.callback(cog, ctx, bob(), settings.max_bet + 1)

        assert (await db.get_account(ALICE)).wallet == 10_000_000

    async def test_startup_refunds_a_match_a_restart_interrupted(self, db, settings):
        await db.add_wallet(ALICE, BET)
        await db.add_wallet(BOB, BET)
        await db.open_escrow("tictactoe", ALICE, BOB, BET)
        cog = Gambling(FakeBot(db, settings))

        await cog.cog_load()

        assert (await db.get_account(ALICE)).wallet == BET
        assert (await db.get_account(BOB)).wallet == BET


class TestChallengeIsShared:
    """The challenge flow is the same view for both head-to-head games, so the
    rules it enforces are tested once in tests/test_matches.py. These check
    that tic-tac-toe is wired into it rather than around it."""

    async def test_the_escrow_records_which_game_holds_the_stakes(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 5_000)
        await cog.tictactoe_command.callback(cog, ctx, bob(), BET)

        await ctx.views[0].apply_accept(bob())

        holds = await db.refund_all_escrow()
        assert [hold.game for hold in holds] == ["tictactoe"]

    async def test_an_open_challenge_can_be_taken_by_anyone(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(CAROL, 5_000)
        await cog.tictactoe_command.callback(cog, ctx, None, BET)

        assert await ctx.views[0].apply_accept(carol()) is None

        match = ctx.views[0].match
        assert {player.id for player in match.players} == {ALICE, CAROL}
