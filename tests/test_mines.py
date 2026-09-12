"""Tests for the mines ruleset and the view that plays it.

The first half is the rules, which import nothing from ``discord`` and are
driven here with plain integers. The load-bearing one is
``TestTheEdgeIsFlatAcrossDepths``: the whole reason mines is safe to offer is
that no stopping rule beats any other, so a retune that makes one depth pay
better than fair has to fail here rather than in a member's balance.

The second half drives the view against a real database, the same way the
blackjack and crash views are covered -- the game-advancing methods take no
Interaction, so most of it needs no stand-in at all.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from flyconomy import economy, mines
from flyconomy.views import MinesView
from tests.conftest import ALICE, BOB
from tests.test_cog_behavior import FakeUser

#: Every playable mine count, so a property is checked over the whole range a
#: member can actually ask for rather than one convenient board.
ALL_MINE_COUNTS = list(range(mines.MIN_MINES, mines.MAX_MINES + 1))


def board(stake: int = 100, *, mine_tiles: set[int]) -> mines.Game:
    """Build a round with the mines in known places."""
    return mines.Game(stake=stake, mine_tiles=frozenset(mine_tiles))


class TestSurvivalProbability:
    def test_an_untouched_board_has_survived(self):
        assert mines.survival_probability(3, 0) == pytest.approx(1.0)

    def test_the_first_press_is_the_share_of_safe_tiles(self):
        assert mines.survival_probability(4, 1) == pytest.approx(12 / 16)

    @pytest.mark.parametrize("mine_count", ALL_MINE_COUNTS)
    def test_survival_falls_with_every_extra_press(self, mine_count):
        chances = [
            mines.survival_probability(mine_count, k)
            for k in range(mines.safe_tiles(mine_count) + 1)
        ]
        assert chances == sorted(chances, reverse=True)

    @pytest.mark.parametrize("mine_count", ALL_MINE_COUNTS)
    def test_clearing_the_board_matches_the_closed_form(self, mine_count):
        # Turning over every safe tile is the same as picking exactly the safe
        # ones out of all the ways the mines could have been placed.
        from math import comb

        cleared = mines.survival_probability(mine_count, mines.safe_tiles(mine_count))
        assert cleared == pytest.approx(1 / comb(mines.TILES, mine_count))


class TestMultiplierFor:
    def test_an_untouched_board_has_nothing_to_cash_out(self):
        assert mines.multiplier_for(3, 0) == 0.0

    @pytest.mark.parametrize("mine_count", ALL_MINE_COUNTS)
    def test_the_multiplier_rises_with_every_safe_press(self, mine_count):
        ladder = [
            mines.multiplier_for(mine_count, k) for k in range(1, mines.safe_tiles(mine_count) + 1)
        ]
        assert ladder == sorted(ladder)

    @pytest.mark.parametrize("mine_count", ALL_MINE_COUNTS)
    def test_no_depth_pays_above_the_cap(self, mine_count):
        for k in range(1, mines.safe_tiles(mine_count) + 1):
            assert mines.multiplier_for(mine_count, k) <= mines.MAX_MULTIPLIER

    def test_more_mines_pay_more_at_the_same_depth(self):
        # The whole point of the mine count: it buys variance, not edge.
        assert mines.multiplier_for(5, 3) > mines.multiplier_for(2, 3)


class TestTheEdgeIsFlatAcrossDepths:
    """The property the game rests on.

    A player choosing when to stop is choosing variance, never edge. If one
    depth ever paid better than another, the game would have a best strategy,
    and a best strategy on a game the house is meant to win is how a casino
    game turns into a faucet.
    """

    @pytest.mark.parametrize("mine_count", ALL_MINE_COUNTS)
    def test_every_uncapped_depth_meets_exactly_the_house_edge(self, mine_count):
        for k in range(1, mines.safe_tiles(mine_count) + 1):
            multiplier = mines.multiplier_for(mine_count, k)
            if multiplier >= mines.MAX_MULTIPLIER:
                continue  # capped depths are covered below
            returned = mines.survival_probability(mine_count, k) * multiplier
            assert returned == pytest.approx(1 - mines.HOUSE_EDGE)

    @pytest.mark.parametrize("mine_count", ALL_MINE_COUNTS)
    def test_no_depth_at_all_beats_the_house_edge(self, mine_count):
        # The other direction, and the one that matters for the anti-abuse
        # invariant: the cap may only ever make a depth worse for the player.
        for k in range(1, mines.safe_tiles(mine_count) + 1):
            returned = mines.survival_probability(mine_count, k) * mines.multiplier_for(
                mine_count, k
            )
            assert returned <= 1 - mines.HOUSE_EDGE + 1e-9

    def test_the_cap_binds_on_a_deep_board(self):
        # If this stops being true the cap has stopped doing anything, and the
        # bound on a cleared board's payout has quietly gone with it.
        deepest = mines.multiplier_for(mines.MAX_MINES, mines.safe_tiles(mines.MAX_MINES))
        assert deepest == mines.MAX_MULTIPLIER

    def test_the_cap_bounds_what_one_round_can_pay(self):
        # The reason the cap exists at all: uncapped, a cleared eight-mine
        # board pays 12,484x, which at the table limit is $1.2B from one press.
        from flyconomy.config import Settings

        limit = Settings(discord_token="placeholder").max_bet
        assert mines.payout(limit, mines.MAX_MULTIPLIER) <= 10_000_000


class TestDeal:
    def test_the_stake_is_recorded(self):
        assert mines.Game.deal(250, 3, random.Random(1)).stake == 250

    @pytest.mark.parametrize("mine_count", ALL_MINE_COUNTS)
    def test_the_requested_mines_are_placed(self, mine_count):
        game = mines.Game.deal(100, mine_count, random.Random(7))
        assert game.mine_count == mine_count

    @pytest.mark.parametrize("mine_count", ALL_MINE_COUNTS)
    def test_every_mine_lands_on_the_board(self, mine_count):
        game = mines.Game.deal(100, mine_count, random.Random(7))
        assert all(0 <= tile < mines.TILES for tile in game.mine_tiles)

    def test_a_fresh_board_has_nothing_turned_over(self):
        game = mines.Game.deal(100, 3, random.Random(7))
        assert game.revealed == []
        assert not game.busted

    @pytest.mark.parametrize("mine_count", [0, mines.MAX_MINES + 1, -1])
    def test_an_unplayable_mine_count_is_refused(self, mine_count):
        with pytest.raises(ValueError, match="mine_count"):
            mines.Game.deal(100, mine_count, random.Random(7))

    def test_the_mines_move_between_deals(self):
        source = random.Random(3)
        placements = {mines.Game.deal(100, 3, source).mine_tiles for _ in range(50)}
        assert len(placements) > 1


class TestReveal:
    def test_a_safe_tile_is_banked(self):
        game = board(mine_tiles={0})
        assert game.reveal(5) is True
        assert game.revealed == [5]
        assert not game.busted

    def test_a_mine_ends_the_round(self):
        game = board(mine_tiles={0})
        assert game.reveal(0) is False
        assert game.busted
        assert game.busted_tile == 0

    def test_a_busted_round_accepts_no_more_presses(self):
        game = board(mine_tiles={0})
        game.reveal(0)
        assert not game.can_reveal(5)

    def test_the_same_tile_cannot_be_pressed_twice(self):
        game = board(mine_tiles={0})
        game.reveal(5)
        assert not game.can_reveal(5)

    @pytest.mark.parametrize("tile", [-1, mines.TILES])
    def test_a_tile_off_the_board_is_refused(self, tile):
        assert not board(mine_tiles={0}).can_reveal(tile)

    def test_the_multiplier_tracks_the_presses(self):
        game = board(mine_tiles={0, 1, 2})
        game.reveal(5)
        game.reveal(6)
        assert game.multiplier == pytest.approx(mines.multiplier_for(3, 2))


class TestExhausted:
    def test_a_live_board_is_not_exhausted(self):
        assert not board(mine_tiles={0, 1, 2}).exhausted

    def test_a_cleared_board_is_exhausted(self):
        game = board(mine_tiles={0})
        for tile in range(1, mines.TILES):
            game.reveal(tile)
        assert game.cleared
        assert game.exhausted

    def test_a_board_at_the_ceiling_is_exhausted(self):
        game = board(mine_tiles=set(range(mines.MAX_MINES)))
        safe = [t for t in range(mines.TILES) if t not in game.mine_tiles]
        for tile in safe:
            game.reveal(tile)
            if game.at_ceiling:
                break
        assert game.at_ceiling
        assert game.exhausted


class TestPayout:
    def test_a_bust_pays_nothing(self):
        assert mines.payout(100, 0.0) == 0

    def test_a_cashout_pays_the_multiplier(self):
        assert mines.payout(100, 2.0) == 200

    def test_rounding_favors_the_house(self):
        assert mines.payout(101, 1.005) == 101


class TestAbandonedMultiplier:
    def test_an_untouched_board_is_refunded(self):
        assert mines.abandoned_multiplier(board(mine_tiles={0})) == mines.REFUND_MULTIPLIER

    def test_a_played_board_cashes_out_where_it_stands(self):
        game = board(mine_tiles={0, 1, 2})
        game.reveal(5)
        assert mines.abandoned_multiplier(game) == pytest.approx(mines.multiplier_for(3, 1))


class TestTileState:
    def test_a_live_board_hides_its_mines(self):
        game = board(mine_tiles={0})
        assert mines.tile_state(game, 0, finished=False) == mines.HIDDEN

    def test_a_settled_board_shows_its_mines(self):
        game = board(mine_tiles={0})
        assert mines.tile_state(game, 0, finished=True) == mines.MINE

    def test_a_turned_tile_reads_as_safe(self):
        game = board(mine_tiles={0})
        game.reveal(5)
        assert mines.tile_state(game, 5, finished=False) == mines.SAFE

    def test_the_mine_that_ended_it_is_marked_apart(self):
        game = board(mine_tiles={0, 1})
        game.reveal(0)
        assert mines.tile_state(game, 0, finished=True) == mines.BUSTED
        assert mines.tile_state(game, 1, finished=True) == mines.MINE


# --------------------------------------------------------------- the view --


@dataclass
class FakeResponse:
    """Records how a button callback answered its interaction."""

    edited: list[Any] = field(default_factory=list)
    ephemeral: list[Any] = field(default_factory=list)

    async def edit_message(self, *, embed: Any = None, view: Any = None) -> None:
        self.edited.append(embed)

    async def send_message(self, *, embed: Any = None, ephemeral: bool = False, **_: Any) -> None:
        self.ephemeral.append(embed)


@dataclass
class FakeInteraction:
    """The parts of an interaction the view touches."""

    user: FakeUser
    response: FakeResponse = field(default_factory=FakeResponse)


class FakeMessage:
    edits: int = 0

    async def edit(self, *, embed: Any = None, view: Any = None) -> None:
        self.edits += 1


@pytest.fixture
def player() -> FakeUser:
    return FakeUser(id=ALICE)


def make_view(db, game: mines.Game, player: FakeUser, **kwargs: Any) -> MinesView:
    return MinesView(db=db, game=game, player=player, timezone="UTC", **kwargs)


class TestViewLayout:
    def test_the_board_fits_discord_s_component_budget(self, db, player):
        # A hard API limit, not a discord.py one: five rows of five. Sixteen
        # tiles take four rows and leave the fifth for Cash Out. This is the
        # check that fails first if the board is ever widened.
        view = make_view(db, board(mine_tiles={0}), player)
        rows: dict[int, int] = {}
        for child in view.children:
            row = child.row
            assert row is not None, "every component is placed on an explicit row"
            rows[row] = rows.get(row, 0) + 1
        assert max(rows) <= 4, "the board spilled past Discord's fifth action row"
        assert all(count <= 5 for count in rows.values()), "a row holds at most five components"

    def test_every_tile_gets_a_button(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        tiles = [c for c in view.children if hasattr(c, "tile")]
        assert sorted(c.tile for c in tiles) == list(range(mines.TILES))


class TestViewSettlement:
    async def test_a_safe_press_pays_nothing_yet(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)

        await view.apply_reveal(5)

        assert (await db.get_account(ALICE)).wallet == 0

    async def test_cashing_out_credits_the_multiplier(self, db, player):
        game = board(mine_tiles={0, 1, 2})
        view = make_view(db, game, player)

        await view.apply_reveal(5)
        await view.apply_cashout()

        assert (await db.get_account(ALICE)).wallet == mines.payout(100, mines.multiplier_for(3, 1))

    async def test_a_mine_takes_the_stake(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)

        await view.apply_reveal(0)

        assert (await db.get_account(ALICE)).wallet == 0

    async def test_settling_twice_pays_once(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)

        await view.settle(multiplier=2.0)
        await view.settle(multiplier=2.0)

        assert (await db.get_account(ALICE)).wallet == 200

    async def test_cashing_out_before_any_press_is_refused(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)

        problem = await view.apply_cashout()

        assert problem is not None
        assert (await db.get_account(ALICE)).wallet == 0

    async def test_a_cleared_board_cashes_out_on_its_own(self, db, player):
        game = board(mine_tiles={0})
        view = make_view(db, game, player)

        for tile in range(1, mines.TILES):
            await view.apply_reveal(tile)

        assert game.cleared
        assert (await db.get_account(ALICE)).wallet == mines.payout(100, game.multiplier)

    async def test_a_board_reaching_the_ceiling_cashes_out_on_its_own(self, db, player):
        # Pressing past the cap can only lose, so the view never leaves that
        # button live. Without this the best play would be to stop guessing,
        # which is a strange thing to ask of a game about pressing buttons.
        game = board(mine_tiles=set(range(mines.MAX_MINES)))
        view = make_view(db, game, player)

        for tile in range(mines.MAX_MINES, mines.TILES):
            await view.apply_reveal(tile)
            if view._settled:
                break

        assert game.at_ceiling
        assert (await db.get_account(ALICE)).wallet == mines.payout(100, mines.MAX_MULTIPLIER)

    async def test_a_press_after_the_round_is_refused(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        await view.apply_reveal(0)

        assert await view.apply_reveal(5) is not None

    async def test_the_same_tile_twice_is_refused(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        await view.apply_reveal(5)

        assert await view.apply_reveal(5) is not None


class TestViewRake:
    async def test_a_bust_feeds_the_pot_and_the_creator(self, db, player):
        view = make_view(
            db,
            board(mine_tiles={0}),
            player,
            rake=0.25,
            creator_tax_rate=0.05,
            creator_tax_user_id=999,
        )

        await view.apply_reveal(0)

        assert (await db.lottery_state()).pot == int(100 * 0.25)
        assert (await db.get_account(999)).bank == economy.STARTING_BANK + int(100 * 0.05)

    async def test_a_win_feeds_neither(self, db, player):
        game = board(mine_tiles={0, 1, 2})
        view = make_view(
            db, game, player, rake=0.25, creator_tax_rate=0.05, creator_tax_user_id=999
        )

        await view.apply_reveal(5)
        await view.apply_cashout()

        assert (await db.lottery_state()).pot == 0
        assert await db.find_account(999) is None


class TestViewButtonState:
    async def test_cash_out_is_dead_until_something_is_turned_over(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        button = next(c for c in view.children if not hasattr(c, "tile"))
        assert button.disabled

    async def test_cash_out_wakes_up_after_a_press(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)

        await view.apply_reveal(5)

        button = next(c for c in view.children if not hasattr(c, "tile"))
        assert not button.disabled

    async def test_a_settled_round_disables_everything(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)

        await view.apply_reveal(0)

        assert all(child.disabled for child in view.children)

    async def test_a_turned_tile_cannot_be_pressed_again(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)

        await view.apply_reveal(5)

        tile = next(c for c in view.children if getattr(c, "tile", None) == 5)
        assert tile.disabled


class TestViewOwnership:
    async def test_the_dealt_player_may_press(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        interaction = FakeInteraction(user=player)

        assert await view.interaction_check(interaction) is True

    async def test_anyone_else_is_turned_away(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        interaction = FakeInteraction(user=FakeUser(id=BOB))

        assert await view.interaction_check(interaction) is False
        assert len(interaction.response.ephemeral) == 1


class TestViewButtonCallbacks:
    async def test_the_cash_out_button_pays_and_redraws(self, db, player):
        game = board(mine_tiles={0, 1, 2})
        view = make_view(db, game, player)
        await view.apply_reveal(5)
        interaction = FakeInteraction(user=player)

        await MinesView.cash_out(view, interaction, None)

        assert (await db.get_account(ALICE)).wallet == mines.payout(100, mines.multiplier_for(3, 1))
        assert len(interaction.response.edited) == 1

    async def test_a_refused_press_is_reported_privately(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        interaction = FakeInteraction(user=player)

        await MinesView.cash_out(view, interaction, None)

        assert len(interaction.response.ephemeral) == 1
        assert not interaction.response.edited

    async def test_a_tile_button_turns_its_own_tile(self, db, player):
        game = board(mine_tiles={0})
        view = make_view(db, game, player)
        tile = next(c for c in view.children if getattr(c, "tile", None) == 5)
        interaction = FakeInteraction(user=player)

        await tile.callback(interaction)

        assert game.revealed == [5]
        assert len(interaction.response.edited) == 1


class TestViewTimeout:
    async def test_a_walked_away_round_cashes_out_rather_than_busting(self, db, player):
        game = board(mine_tiles={0, 1, 2})
        view = make_view(db, game, player)
        view.message = FakeMessage()
        await view.apply_reveal(5)

        await view.on_timeout()

        assert (await db.get_account(ALICE)).wallet == mines.payout(100, mines.multiplier_for(3, 1))
        assert view.message.edits == 1

    async def test_an_untouched_round_is_refunded_whole(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        view.message = FakeMessage()

        await view.on_timeout()

        assert (await db.get_account(ALICE)).wallet == 100

    async def test_a_timeout_after_settling_pays_nothing_more(self, db, player):
        view = make_view(db, board(mine_tiles={0}), player)
        await view.apply_reveal(0)

        await view.on_timeout()

        assert (await db.get_account(ALICE)).wallet == 0
