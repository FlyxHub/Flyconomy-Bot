"""Tests for the blackjack view and the command that posts it.

The view's game-advancing methods take no Interaction, so most of this drives
them directly against a real database. The button callbacks and the ownership
check are covered with a stand-in interaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from flyconomy import blackjack, economy, embeds
from flyconomy.blackjack import Game, Outcome
from flyconomy.cogs.gambling import Gambling
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import InsufficientFundsError
from flyconomy.views import BlackjackView
from tests.conftest import ALICE, BOB
from tests.test_blackjack import ACE, KING, hand
from tests.test_cog_behavior import FakeBot, FakeContext, FakeUser


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
def player() -> FakeUser:
    return FakeUser(id=ALICE)


def make_view(db: Database, game: Game, player: FakeUser, base_bet: int = 100) -> BlackjackView:
    """Build a view around an already-dealt hand."""
    return BlackjackView(db=db, game=game, player=player, base_bet=base_bet, timezone="UTC")


def live_game(stake: int = 100) -> Game:
    """A hand the player can still act on: 16 against a dealer 9."""
    # The shoe is drawn from the end, so the 2 comes out first and leaves the
    # player on 18: still live, neither bust nor 21.
    return Game(
        player=hand(10, 6),
        dealer=hand(9, 7),
        shoe=hand(5, 4, 3, 8, 7, 2),
        stake=stake,
    )


class TestSettlement:
    async def test_a_win_credits_the_payout(self, db, player):
        game = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        view = make_view(db, game, player)

        await view.apply_stand()

        assert game.outcome == Outcome.PLAYER_WINS
        assert (await db.get_account(ALICE)).wallet == 200

    async def test_a_loss_credits_nothing(self, db, player):
        game = Game(player=hand(10, 6), dealer=hand(10, 8), shoe=hand(2), stake=100)
        view = make_view(db, game, player)

        await view.apply_stand()

        assert game.outcome == Outcome.DEALER_WINS
        assert (await db.get_account(ALICE)).wallet == 0

    async def test_a_push_returns_the_stake(self, db, player):
        game = Game(player=hand(10, 8), dealer=hand(10, 8), shoe=hand(2), stake=100)
        view = make_view(db, game, player)

        await view.apply_stand()

        assert game.outcome == Outcome.PUSH
        assert (await db.get_account(ALICE)).wallet == 100

    async def test_settling_twice_only_pays_once(self, db, player):
        # A click and the timeout can both reach settle(); only one may pay.
        game = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        view = make_view(db, game, player)

        await view.apply_stand()
        await view.settle()
        await view.settle()

        assert (await db.get_account(ALICE)).wallet == 200

    async def test_settling_an_unfinished_hand_does_nothing(self, db, player):
        view = make_view(db, live_game(), player)

        await view.settle()

        assert (await db.get_account(ALICE)).wallet == 0
        assert not view.is_finished()


class TestCreatorTax:
    async def test_a_loss_pays_the_creator_without_touching_the_pot(self, db, player):
        game = Game(player=hand(10, 6), dealer=hand(10, 8), shoe=hand(2), stake=100)
        view = BlackjackView(
            db=db,
            game=game,
            player=player,
            base_bet=100,
            timezone="UTC",
            rake=0.25,
            creator_tax_rate=0.05,
            creator_tax_user_id=999,
        )

        await view.apply_stand()

        assert (await db.lottery_state()).pot == int(100 * 0.25)
        assert (await db.get_account(999)).bank == economy.STARTING_BANK + int(100 * 0.05)

    async def test_a_win_pays_the_creator_nothing(self, db, player):
        game = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        view = BlackjackView(
            db=db,
            game=game,
            player=player,
            base_bet=100,
            timezone="UTC",
            creator_tax_rate=0.05,
            creator_tax_user_id=999,
        )

        await view.apply_stand()

        assert await db.find_account(999) is None

    async def test_an_unset_creator_id_pays_nobody(self, db, player):
        game = Game(player=hand(10, 6), dealer=hand(10, 8), shoe=hand(2), stake=100)
        view = BlackjackView(
            db=db, game=game, player=player, base_bet=100, timezone="UTC", creator_tax_rate=0.05
        )

        await view.apply_stand()

        # No wallet was configured, so nothing outside the player's own account
        # is touched even though the rate is nonzero.
        assert (await db.get_account(ALICE)).wallet == 0


class TestHitAndStand:
    async def test_hitting_draws_a_card(self, db, player):
        game = live_game()
        view = make_view(db, game, player)

        await view.apply_hit()

        assert len(game.player) == 3

    async def test_busting_ends_the_hand_and_pays_nothing(self, db, player):
        game = Game(player=hand(10, 6), dealer=hand(9, 7), shoe=hand(KING), stake=100)
        view = make_view(db, game, player)

        await view.apply_hit()

        assert game.outcome == Outcome.PLAYER_BUST
        assert (await db.get_account(ALICE)).wallet == 0

    async def test_a_dealer_bust_pays_the_player(self, db, player):
        game = Game(player=hand(10, 8), dealer=hand(10, 6), shoe=hand(KING), stake=100)
        view = make_view(db, game, player)

        await view.apply_stand()

        assert game.outcome == Outcome.DEALER_BUST
        assert (await db.get_account(ALICE)).wallet == 200


class TestDoubleDown:
    async def test_doubling_debits_a_second_stake(self, db, player):
        game = Game(player=hand(6, 5), dealer=hand(10, 7), shoe=hand(2, 9), stake=100)
        view = make_view(db, game, player)
        await db.add_wallet(ALICE, 100)

        problem = await view.apply_double()

        assert problem is None
        assert game.stake == 200
        assert game.doubled
        # 20 beats the dealer's 17, returning twice the doubled stake.
        assert game.outcome == Outcome.PLAYER_WINS
        assert (await db.get_account(ALICE)).wallet == 400

    async def test_a_doubled_loss_costs_both_stakes(self, db, player):
        game = Game(player=hand(6, 5), dealer=hand(10, 9), shoe=hand(9, 3), stake=100)
        view = make_view(db, game, player)
        await db.add_wallet(ALICE, 100)

        await view.apply_double()

        assert game.outcome == Outcome.DEALER_WINS
        assert (await db.get_account(ALICE)).wallet == 0

    async def test_doubling_without_the_money_is_refused(self, db, player):
        game = live_game()
        view = make_view(db, game, player)

        problem = await view.apply_double()

        assert problem is not None
        assert "wallet only holds" in problem
        # The hand is untouched, so no free card was dealt.
        assert len(game.player) == 2
        assert not game.doubled
        assert not game.finished

    async def test_doubling_after_a_hit_is_refused(self, db, player):
        game = live_game()
        view = make_view(db, game, player)
        await db.add_wallet(ALICE, 1_000)
        await view.apply_hit()

        if not game.finished:
            problem = await view.apply_double()
            assert problem == "You can only double down on your first two cards."

    async def test_a_refused_double_does_not_charge(self, db, player):
        game = live_game()
        view = make_view(db, game, player)
        await db.add_wallet(ALICE, 1_000)
        await view.apply_stand()
        before = (await db.get_account(ALICE)).wallet

        problem = await view.apply_double()

        assert problem is not None
        assert (await db.get_account(ALICE)).wallet == before


class TestButtonState:
    async def test_a_live_hand_enables_every_button(self, db, player):
        view = make_view(db, live_game(), player)
        assert not any(child.disabled for child in view.children)

    async def test_a_finished_hand_disables_every_button(self, db, player):
        game = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        view = make_view(db, game, player)

        await view.apply_stand()

        assert all(child.disabled for child in view.children)

    async def test_doubling_is_disabled_after_a_hit(self, db, player):
        game = live_game()
        view = make_view(db, game, player)

        await view.apply_hit()

        double = next(c for c in view.children if c.custom_id == "blackjack:double")
        assert double.disabled


class TestOwnership:
    async def test_the_dealt_player_may_press(self, db, player):
        view = make_view(db, live_game(), player)
        interaction = FakeInteraction(user=player)

        assert await view.interaction_check(interaction) is True
        assert not interaction.response.ephemeral

    async def test_anyone_else_is_turned_away(self, db, player):
        view = make_view(db, live_game(), player)
        interaction = FakeInteraction(user=FakeUser(id=BOB))

        assert await view.interaction_check(interaction) is False
        assert len(interaction.response.ephemeral) == 1

    async def test_a_rejected_press_does_not_move_the_hand(self, db, player):
        game = live_game()
        view = make_view(db, game, player)

        await view.interaction_check(FakeInteraction(user=FakeUser(id=BOB)))

        assert len(game.player) == 2
        assert not game.finished


class TestButtonCallbacks:
    async def test_the_hit_button_draws_and_redraws(self, db, player):
        game = live_game()
        view = make_view(db, game, player)
        interaction = FakeInteraction(user=player)

        await BlackjackView.hit(view, interaction, None)

        assert len(game.player) == 3
        assert len(interaction.response.edited) == 1

    async def test_the_stand_button_finishes_the_hand(self, db, player):
        game = live_game()
        view = make_view(db, game, player)
        interaction = FakeInteraction(user=player)

        await BlackjackView.stand(view, interaction, None)

        assert game.finished
        assert len(interaction.response.edited) == 1

    async def test_the_double_button_reports_a_refusal_privately(self, db, player):
        game = live_game()
        view = make_view(db, game, player)
        interaction = FakeInteraction(user=player)

        await BlackjackView.double(view, interaction, None)

        # Refused for lack of funds, so the hand is unchanged and nothing
        # was redrawn publicly.
        assert len(interaction.response.ephemeral) == 1
        assert not interaction.response.edited
        assert not game.doubled


class TestTimeout:
    async def test_a_timeout_stands_and_pays_out(self, db, player):
        game = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        view = make_view(db, game, player)
        view.message = _FakeMessage()

        await view.on_timeout()

        assert game.finished
        assert (await db.get_account(ALICE)).wallet == 200
        assert view.message.edits == 1

    async def test_a_timeout_on_a_finished_hand_changes_nothing(self, db, player):
        game = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        view = make_view(db, game, player)
        await view.apply_stand()
        view.message = _FakeMessage()

        await view.on_timeout()

        assert (await db.get_account(ALICE)).wallet == 200
        assert view.message.edits == 0

    async def test_a_timeout_without_a_message_still_settles(self, db, player):
        game = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        view = make_view(db, game, player)

        await view.on_timeout()

        assert (await db.get_account(ALICE)).wallet == 200

    def test_the_timeout_matches_the_documented_window(self, db, player):
        view = make_view(db, live_game(), player)
        assert view.timeout == blackjack.DECISION_TIMEOUT_SECONDS


class TestBlackjackCommand:
    async def test_the_stake_is_debited_on_the_deal(self, db, settings):
        cog = Gambling(FakeBot(db, settings))
        ctx = FakeContext(author=FakeUser(id=ALICE))
        await db.add_wallet(ALICE, 1_000)

        await cog.blackjack_command.callback(cog, ctx, 100)

        # Either the hand is live and 100 is staked, or a natural already paid.
        assert (await db.get_account(ALICE)).wallet != 1_000

    async def test_a_live_hand_posts_buttons(self, db, settings):
        cog = Gambling(FakeBot(db, settings))
        ctx = FakeContext(author=FakeUser(id=ALICE))
        await db.add_wallet(ALICE, 100_000)

        for _ in range(40):
            ctx.views.clear()
            await cog.blackjack_command.callback(cog, ctx, 100)
            if ctx.views:
                view = ctx.views[-1]
                assert isinstance(view, BlackjackView)
                assert not view.game.finished
                return
        pytest.fail("no live hand was dealt in 40 tries")

    async def test_a_natural_resolves_without_buttons(self, db, settings, player):
        cog = Gambling(FakeBot(db, settings))
        ctx = FakeContext(author=FakeUser(id=ALICE))
        await db.add_wallet(ALICE, 1_000)

        # Force a natural by rigging the deal.
        cog.rng = _NaturalDeal()
        await cog.blackjack_command.callback(cog, ctx, 100)

        assert not ctx.views, "a decided hand must not offer buttons"
        assert ctx.embeds
        # 1,000 - 100 staked + 250 returned for a 3:2 natural.
        assert (await db.get_account(ALICE)).wallet == 1_150

    async def test_a_bet_beyond_the_wallet_is_refused(self, db, settings):
        cog = Gambling(FakeBot(db, settings))
        ctx = FakeContext(author=FakeUser(id=ALICE))
        await db.add_wallet(ALICE, 10)

        with pytest.raises(InsufficientFundsError):
            await cog.blackjack_command.callback(cog, ctx, 11)

        assert (await db.get_account(ALICE)).wallet == 10


@dataclass
class _FakeMessage:
    edits: int = 0

    async def edit(self, *, embed: Any = None, view: Any = None) -> None:
        self.edits += 1


class _NaturalDeal:
    """A random source whose deal gives the player a natural."""

    def sample(self, population: Any, k: int, *, counts: Any = None) -> list[Any]:
        scripted = hand(ACE, KING)
        rest = [c for c in population if c not in scripted]
        # Game.deal pops from the end, so the player's two cards go last.
        return [*rest[: k - len(scripted)], *reversed(scripted)]


class TestBlackjackEmbed:
    def test_the_hole_card_is_hidden_while_the_hand_is_live(self, player):
        embed = embeds.blackjack_embed(live_game(), player, "UTC")
        dealer = next(f for f in embed.fields if f.name == "Dealer")
        assert "??" in dealer.value
        # The face-down card must not leak into the embed.
        assert str(live_game().dealer[1]) not in dealer.value

    def test_the_hole_card_is_revealed_once_the_hand_ends(self, player):
        game = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        game.stand()

        embed = embeds.blackjack_embed(game, player, "UTC")
        dealer = next(f for f in embed.fields if f.name == "Dealer")
        assert "??" not in dealer.value
        assert str(game.dealer[1]) in dealer.value

    def test_a_soft_hand_is_marked(self, player):
        game = Game(player=hand(ACE, 5), dealer=hand(9, 7), shoe=hand(2), stake=100)
        embed = embeds.blackjack_embed(game, player, "UTC")
        you = next(f for f in embed.fields if f.name == "You")
        assert "soft" in you.value

    def test_a_hard_hand_is_not_marked(self, player):
        embed = embeds.blackjack_embed(live_game(), player, "UTC")
        you = next(f for f in embed.fields if f.name == "You")
        assert "soft" not in you.value

    def test_a_doubled_stake_is_labelled(self, player):
        game = Game(player=hand(6, 5), dealer=hand(10, 7), shoe=hand(2, 9), stake=100)
        game.double_down()
        embed = embeds.blackjack_embed(game, player, "UTC")
        assert any("doubled" in f.name.lower() for f in embed.fields)

    def test_a_live_hand_prompts_for_an_action(self, player):
        embed = embeds.blackjack_embed(live_game(), player, "UTC")
        assert embed.footer.text is not None
        assert not any(f.name == "Result" for f in embed.fields)

    def test_a_loss_is_coloured_differently_from_a_win(self, player):
        lost = Game(player=hand(10, 6), dealer=hand(10, 8), shoe=hand(2), stake=100)
        lost.stand()
        won = Game(player=hand(10, 9), dealer=hand(10, 7), shoe=hand(2), stake=100)
        won.stand()

        assert (
            embeds.blackjack_embed(lost, player, "UTC").color
            != embeds.blackjack_embed(won, player, "UTC").color
        )

    @pytest.mark.parametrize(
        ("outcome", "fragment"),
        [
            (Outcome.PLAYER_BLACKJACK, "Blackjack!"),
            (Outcome.DEALER_BUST, "Dealer busts"),
            (Outcome.PLAYER_WINS, "You win"),
            (Outcome.PUSH, "Push"),
            (Outcome.PLAYER_BUST, "Bust"),
            (Outcome.DEALER_WINS, "Dealer wins"),
        ],
    )
    def test_every_outcome_has_its_own_line(self, outcome, fragment):
        game = live_game()
        game.outcome = outcome
        assert fragment in embeds.blackjack_result_line(game)

    def test_a_result_line_needs_a_finished_hand(self):
        with pytest.raises(ValueError, match="still in play"):
            embeds.blackjack_result_line(live_game())

    def test_the_natural_line_quotes_the_three_to_two_payout(self):
        game = live_game(stake=100)
        game.outcome = Outcome.PLAYER_BLACKJACK
        assert "$250" in embeds.blackjack_result_line(game)
