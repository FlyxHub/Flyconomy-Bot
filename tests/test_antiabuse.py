"""Tests for the exploit defences.

Each class here corresponds to a hole that was found by measuring the economy,
so these are regression tests: if one fails, that exploit is open again.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import AsyncIterator
from fractions import Fraction
from pathlib import Path

import pytest

from flyconomy import crash, economy, jackpot, tictactoe
from flyconomy.bot import describe_command_error
from flyconomy.cogs.base import BaseCog
from flyconomy.cogs.gambling import Gambling
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import BetTooLargeError, RateLimitedError
from flyconomy.ratelimit import SlidingWindowLimiter
from tests.conftest import ALICE, BOB
from tests.test_cog_behavior import FakeBot, FakeContext, FakeUser

#: Every wagering command, as (name, arguments before the stake).
WAGER_COMMANDS = [
    ("coinflip", ("heads",)),
    ("rps", ("rock",)),
    ("dice", (3,)),
    ("slots", ()),
    ("war", ()),
    ("blackjack_command", ()),
    ("crash_command", ()),
    ("jackpot_command", ()),
    ("tictactoe_command", (FakeUser(id=BOB),)),
]


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


class TestNoGameIsProfitable:
    """The root cause. A game with a positive expected value is a money printer
    that no rate limit can close, because the profit is per play rather than
    per second."""

    def test_coinflip_is_fair(self):
        assert 0.5 * (economy.COINFLIP_RETURN - 1) + 0.5 * -1 == 0

    def test_dice_is_fair(self):
        ev = (1 / 6) * (economy.DICE_RETURN - 1) + (5 / 6) * -1
        assert ev == pytest.approx(0)

    def test_rps_is_fair(self):
        # Version 1 refunded the tie, which paid +33.33%.
        ev = (
            (1 / 3) * (economy.RPS_RETURN - 1)
            + (1 / 3) * (economy.RPS_TIE_RETURN - 1)
            + (1 / 3) * -1
        )
        assert ev == pytest.approx(0)

    def test_an_rps_tie_is_not_refunded(self):
        assert economy.RPS_TIE_RETURN == 0

    def test_war_is_fair(self):
        hands = list(itertools.permutations(economy.DECK, 2))
        assert sum(economy.war_payout_multiplier(p, d) - 1 for p, d in hands) == 0

    def test_slots_favours_the_house(self):
        spins = list(itertools.product(economy.SLOT_REEL, repeat=economy.SLOT_REEL_COUNT))
        rtp = Fraction(sum(economy.slots_payout_multiplier(s) for s in spins), len(spins))
        assert rtp < 1

    @pytest.mark.parametrize("bet", ["red", 7])
    def test_roulette_favours_the_house(self, bet):
        wheel = economy.ROULETTE_WHEEL
        ev = sum(economy.roulette_payout_multiplier(bet, p) - 1 for p in wheel) / len(wheel)
        assert ev < 0

    @pytest.mark.parametrize("target", [1.5, 2.0, 5.0, 10.0])
    def test_crash_favours_the_house_at_every_target(self, target):
        """Unlike blackjack, crash's edge is provably flat across every
        cash-out strategy -- see crash.Game.deal's docstring for the
        derivation. This is a coarse regression check against the actual
        sampler; the detailed magnitude proof lives in tests/test_crash.py."""
        rng = random.Random(99)
        stake = 100
        staked = 0
        returned = 0
        for _ in range(100_000):
            game = crash.Game.deal(stake, rng)
            staked += game.stake
            if game.crash_point >= target:
                returned += crash.payout(game.stake, target)
        edge = (staked - returned) / staked
        assert edge > 0, f"target {target}x gave the player an edge"

    @pytest.mark.parametrize("bet", [1, 100, 10_000, 1_000_000])
    @pytest.mark.parametrize("game", [tictactoe])
    def test_a_head_to_head_match_cannot_pay_out_more_than_was_staked(self, game, bet):
        """A match is zero-sum before the cut however well anybody plays: the
        two stakes are the whole pot, and the winner takes less than it. Skill
        decides which player the money moves to, never how much there is."""
        assert game.payout(bet * 2) <= bet * 2

    @pytest.mark.parametrize(
        "antes", [(100, 100), (100, 900), (1, 1_000_000), (500, 300, 200), (10_000,) * 8]
    )
    def test_the_jackpot_is_negative_for_every_entrant(self, antes):
        """A player-funded pot is the one game where the players are each
        other's opposition, so it cannot print money however the pot is split:
        the payout is always smaller than the antes that made it. Weighting the
        odds by ante keeps every entrant on the same edge as well as below
        zero -- tests/test_jackpot.py makes that argument in full."""
        pot = sum(antes)
        for ante in antes:
            profit = jackpot.win_chance(ante, pot) * jackpot.payout(pot) - ante
            assert profit <= 0, f"an ante of {ante} in a pot of {pot} profits {profit:+.2f}"

    def test_no_game_pays_more_than_it_takes(self):
        """The property that actually matters: nothing has a positive edge."""
        edges = {
            "coinflip": 0.5 * (economy.COINFLIP_RETURN - 1) + 0.5 * -1,
            "dice": (1 / 6) * (economy.DICE_RETURN - 1) + (5 / 6) * -1,
            "rps": (1 / 3) * (economy.RPS_RETURN - 1)
            + (1 / 3) * (economy.RPS_TIE_RETURN - 1)
            + (1 / 3) * -1,
            "jackpot": (jackpot.win_chance(10_000, 20_000) * jackpot.payout(20_000) - 10_000)
            / 10_000,
            # Two evenly matched players each win half the time.
            "tictactoe": (0.5 * tictactoe.payout(20_000) - 10_000) / 10_000,
        }
        for name, edge in edges.items():
            assert edge <= 1e-9, f"{name} pays players {edge:+.4f} per unit staked"


class TestFaucetsAreThrottled:
    def test_begging_cannot_outpace_a_maximum_level_miner(self):
        beg_per_hour = (
            (1 / economy.BEG_SUCCESS_ODDS)
            * ((economy.BEG_MIN + economy.BEG_MAX) / 2)
            * (3600 / economy.BEG_COOLDOWN_SECONDS)
        )
        mine_per_hour = economy.FLX_PRICE * (
            economy.MINE_CHANCE_PERCENT[economy.MAX_MINER_LEVEL] / 100
        )
        assert beg_per_hour < mine_per_hour

    def test_every_faucet_has_a_cooldown(self):
        for seconds in (
            economy.BEG_COOLDOWN_SECONDS,
            economy.MINE_COOLDOWN_SECONDS,
            economy.DAILY_COOLDOWN_SECONDS,
        ):
            assert seconds > 0


class TestTableLimit:
    async def test_a_bet_at_the_limit_is_accepted(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, settings.max_bet)

        await cog.coinflip.callback(cog, ctx, "heads", settings.max_bet)

        assert ctx.sent

    @pytest.mark.parametrize(("command", "leading"), WAGER_COMMANDS)
    async def test_every_game_refuses_a_bet_over_the_limit(
        self, db, settings, ctx, command, leading
    ):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)
        over = settings.max_bet + 1

        with pytest.raises(BetTooLargeError):
            await getattr(cog, command).callback(cog, ctx, *leading, over)

    @pytest.mark.parametrize(("command", "leading"), WAGER_COMMANDS)
    async def test_a_refused_bet_costs_nothing(self, db, settings, ctx, command, leading):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)
        before = (await db.get_account(ALICE)).wallet

        with pytest.raises(BetTooLargeError):
            await getattr(cog, command).callback(cog, ctx, *leading, settings.max_bet + 1)

        assert (await db.get_account(ALICE)).wallet == before

    async def test_roulette_refuses_an_oversized_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)

        with pytest.raises(BetTooLargeError):
            await cog.roulette.callback(cog, ctx, "red", settings.max_bet + 1)

        assert (await db.get_account(ALICE)).wallet == 10_000_000

    async def test_the_limit_is_configurable(self, db, ctx):
        tight = Settings(discord_token="placeholder", max_bet=500)
        cog = Gambling(FakeBot(db, tight))
        await db.add_wallet(ALICE, 10_000)

        with pytest.raises(BetTooLargeError):
            await cog.coinflip.callback(cog, ctx, "heads", 501)

    async def test_the_refusal_names_the_limit(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10_000_000)

        with pytest.raises(BetTooLargeError) as caught:
            await cog.coinflip.callback(cog, ctx, "heads", settings.max_bet + 1)

        message = describe_command_error(caught.value)
        assert message is not None
        assert f"{settings.max_bet:,}" in message


class TestSharedRateLimit:
    """A per-command cooldown can be dodged by rotating between commands, so the
    budget is shared across every game command."""

    @staticmethod
    def _cog(db: Database, settings: Settings, limiter: SlidingWindowLimiter) -> BaseCog:
        return Gambling(FakeBot(db, settings, limiter))

    async def test_actions_within_the_budget_pass(self, db, settings, ctx):
        limiter = SlidingWindowLimiter(rate=6, per=10)
        cog = self._cog(db, settings, limiter)
        for _ in range(6):
            assert await cog.cog_check(ctx) is True

    async def test_the_action_past_the_budget_is_refused(self, db, settings, ctx):
        limiter = SlidingWindowLimiter(rate=2, per=10)
        cog = self._cog(db, settings, limiter)
        await cog.cog_check(ctx)
        await cog.cog_check(ctx)

        with pytest.raises(RateLimitedError):
            await cog.cog_check(ctx)

    async def test_the_budget_is_shared_across_commands(self, db, settings, ctx):
        # Rotating between games must not multiply the allowance.
        limiter = SlidingWindowLimiter(rate=3, per=10)
        gambling = self._cog(db, settings, limiter)
        economy_cog = Gambling(gambling.bot)

        await gambling.cog_check(ctx)
        await economy_cog.cog_check(ctx)
        await gambling.cog_check(ctx)

        with pytest.raises(RateLimitedError):
            await economy_cog.cog_check(ctx)

    async def test_one_member_cannot_lock_out_another(self, db, settings):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        cog = self._cog(db, settings, limiter)

        await cog.cog_check(FakeContext(author=FakeUser(id=ALICE)))
        assert await cog.cog_check(FakeContext(author=FakeUser(id=BOB))) is True

    async def test_the_refusal_tells_the_member_how_long_to_wait(self, db, settings, ctx):
        limiter = SlidingWindowLimiter(rate=1, per=10)
        cog = self._cog(db, settings, limiter)
        await cog.cog_check(ctx)

        with pytest.raises(RateLimitedError) as caught:
            await cog.cog_check(ctx)

        message = describe_command_error(caught.value)
        assert message is not None
        assert "too quickly" in message

    async def test_the_limit_comes_from_settings(self, db):
        configured = Settings(
            discord_token="placeholder", rate_limit_actions=2, rate_limit_seconds=30
        )
        limiter = SlidingWindowLimiter(
            rate=configured.rate_limit_actions, per=configured.rate_limit_seconds
        )
        assert limiter.rate == 2
        assert limiter.per == 30


class TestRefundedCooldownsCannotLoop:
    """`mine` without a miner and `rob` on an empty wallet refund their own
    cooldown, so the shared budget is the only thing stopping a free loop."""

    async def test_mining_without_a_miner_still_spends_budget(self, db, settings, ctx):
        from flyconomy.cogs.mining import Mining

        cog = Mining(FakeBot(db, settings, SlidingWindowLimiter(rate=2, per=10)))

        await cog.cog_check(ctx)
        await cog.cog_check(ctx)
        with pytest.raises(RateLimitedError):
            await cog.cog_check(ctx)

    async def test_robbing_an_empty_wallet_still_spends_budget(self, db, settings, ctx):
        from flyconomy.cogs.economy import Economy

        cog = Economy(FakeBot(db, settings, SlidingWindowLimiter(rate=1, per=10)))

        await cog.cog_check(ctx)
        with pytest.raises(RateLimitedError):
            await cog.cog_check(ctx)


class TestGrindingIsNotProfitable:
    """The end-to-end property: playing a lot must not create money."""

    @pytest.mark.parametrize("game", ["coinflip", "rps", "dice", "slots", "war"])
    async def test_a_long_session_does_not_print_money(self, db, settings, ctx, game):
        cog = Gambling(FakeBot(db, settings))
        cog.rng.seed(2024)
        stake = 100
        bankroll = 5_000_000
        await db.add_wallet(ALICE, bankroll)

        args = {"coinflip": ("heads",), "rps": ("rock",), "dice": (3,)}.get(game, ())
        for _ in range(4_000):
            ctx.sent.clear()
            await getattr(cog, game).callback(cog, ctx, *args, stake)

        final = (await db.get_account(ALICE)).wallet
        staked = 4_000 * stake
        # Variance is real, so this bounds the edge rather than demanding a loss.
        assert (final - bankroll) / staked < 0.05, f"{game} paid out too well"
