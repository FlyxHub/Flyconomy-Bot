"""Tests that exercise the command bodies against a real database.

The cogs are invoked through their callbacks with a stand-in context, so these
cover the branching each command does without needing a gateway connection.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from flyconomy import economy
from flyconomy.cogs.economy import Economy
from flyconomy.cogs.gambling import Gambling
from flyconomy.cogs.mining import Mining
from flyconomy.config import Settings
from flyconomy.database import Database
from flyconomy.errors import InsufficientFundsError
from flyconomy.ratelimit import SlidingWindowLimiter
from tests.conftest import ALICE, BOB


@dataclass
class FakeUser:
    """The parts of a member the commands actually touch."""

    id: int
    display_name: str = "Tester"
    bot: bool = False

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    @property
    def display_avatar(self) -> Any:
        return type("Asset", (), {"url": "https://example.invalid/avatar.png"})()


@dataclass
class FakeCommand:
    """Records whether a command reset its own cooldown."""

    cooldown_reset: bool = False

    def reset_cooldown(self, _ctx: object) -> None:
        self.cooldown_reset = True


@dataclass
class FakeMessage:
    """A stand-in for the message a command posted, which a view later edits."""

    embed: Any = None
    view: Any = None
    edits: int = 0

    async def edit(self, *, embed: Any = None, view: Any = None) -> None:
        self.embed = embed
        self.view = view
        self.edits += 1


@dataclass
class FakeContext:
    """Captures what a command sent instead of talking to Discord."""

    author: FakeUser
    sent: list[str] = field(default_factory=list)
    embeds: list[Any] = field(default_factory=list)
    views: list[Any] = field(default_factory=list)
    command: FakeCommand = field(default_factory=FakeCommand)

    async def send(
        self, content: str | None = None, *, embed: Any = None, view: Any = None, **_: Any
    ) -> FakeMessage:
        if content is not None:
            self.sent.append(content)
        if embed is not None:
            self.embeds.append(embed)
        if view is not None:
            self.views.append(view)
        return FakeMessage(embed=embed, view=view)

    @property
    def last(self) -> str:
        return self.sent[-1]

    @property
    def text(self) -> str:
        return " ".join(self.sent)


class FakeBot:
    """The bot surface the cogs read: a database, settings, and a rate limiter."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        limiter: SlidingWindowLimiter | None = None,
        channels: dict[int, Any] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.limiter = limiter or SlidingWindowLimiter(
            rate=settings.rate_limit_actions, per=settings.rate_limit_seconds
        )
        self._channels = channels or {}

    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)


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


class ScriptedRandom(random.Random):
    """A random source with a predetermined result, to pin down one outcome.

    Anything not scripted falls through to real randomness.
    """

    def __init__(
        self,
        *,
        choice: Any = None,
        randint: int | None = None,
        sample: list[Any] | None = None,
    ) -> None:
        super().__init__()
        self._choice = choice
        self._randint = randint
        self._sample = sample

    def choice(self, seq: Any) -> Any:
        return super().choice(seq) if self._choice is None else self._choice

    def randint(self, a: int, b: int) -> int:
        return super().randint(a, b) if self._randint is None else self._randint

    def sample(self, population: Any, k: int, *, counts: Any = None) -> list[Any]:
        if self._sample is None:
            return super().sample(population, k, counts=counts)
        return self._sample[:k]


class CyclingRandom(random.Random):
    """A random source that returns scripted choices in order, then repeats.

    Slots call choice() once per reel, so this scripts a whole spin.
    """

    def __init__(self, *, choices: list[Any]) -> None:
        super().__init__()
        self._choices = choices
        self._index = 0

    def choice(self, seq: Any) -> Any:
        value = self._choices[self._index % len(self._choices)]
        self._index += 1
        return value


def _seeded(cog: Any, seed: int) -> Any:
    """Give a cog a deterministic random source."""
    cog.rng = random.Random(seed)
    return cog


class TestBanking:
    async def test_deposit_moves_the_named_amount(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_wallet(ALICE, 500)

        await cog.deposit.callback(cog, ctx, 200)

        account = await db.get_account(ALICE)
        assert account.wallet == 300
        assert "200" in ctx.last

    async def test_deposit_defaults_to_the_whole_wallet(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_wallet(ALICE, 750)

        await cog.deposit.callback(cog, ctx, None)

        account = await db.get_account(ALICE)
        assert account.wallet == 0
        assert account.bank == economy.STARTING_BANK + 750

    async def test_depositing_an_empty_wallet_explains_itself(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))

        await cog.deposit.callback(cog, ctx, None)

        assert "empty" in ctx.last.lower()

    async def test_depositing_more_than_you_hold_is_refused(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_wallet(ALICE, 100)

        with pytest.raises(InsufficientFundsError):
            await cog.deposit.callback(cog, ctx, 101)

        assert (await db.get_account(ALICE)).wallet == 100

    async def test_withdraw_defaults_to_the_whole_bank(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))

        await cog.withdraw.callback(cog, ctx, None)

        account = await db.get_account(ALICE)
        assert account.bank == 0
        assert account.wallet == economy.STARTING_BANK


class TestResetMe:
    async def test_resetme_deletes_the_caller_account(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)

        await cog.resetme.callback(cog, ctx)

        assert await db.find_account(ALICE) is None
        assert "reset" in ctx.last.lower()

    async def test_resetme_leaves_other_members_alone(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 500)

        await cog.resetme.callback(cog, ctx)

        assert (await db.get_account(BOB)).wallet == 500

    async def test_resetting_with_no_account_says_so(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))

        await cog.resetme.callback(cog, ctx)

        assert "don't have an account" in ctx.last.lower()


class TestIncome:
    async def test_beg_either_pays_or_says_nothing(self, db, settings, ctx):
        cog = _seeded(Economy(FakeBot(db, settings)), 7)

        for _ in range(20):
            await cog.beg.callback(cog, ctx)

        assert (await db.get_account(ALICE)).wallet >= 0
        assert any("nothing" in message for message in ctx.sent)
        assert any("$" in message for message in ctx.sent)

    async def test_beg_never_pays_more_than_the_cap(self, db, settings, ctx):
        cog = _seeded(Economy(FakeBot(db, settings)), 3)

        for _ in range(30):
            await cog.beg.callback(cog, ctx)

        assert (await db.get_account(ALICE)).wallet <= 30 * economy.BEG_MAX

    async def test_daily_pays_ten_percent_of_the_bank(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_bank(ALICE, 9_000)

        await cog.daily.callback(cog, ctx)

        assert (await db.get_account(ALICE)).bank == 11_000

    async def test_daily_on_an_empty_bank_pays_nothing_without_failing(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.transfer(ALICE, source="bank", destination="wallet", amount=1_000)

        await cog.daily.callback(cog, ctx)

        assert (await db.get_account(ALICE)).bank == 0
        assert "$0" in ctx.last


class TestRob:
    async def test_robbing_yourself_is_refused_and_takes_nothing(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_wallet(ALICE, 500)

        await cog.rob.callback(cog, ctx, FakeUser(id=ALICE))

        assert "cannot rob yourself" in ctx.last.lower()
        assert (await db.get_account(ALICE)).wallet == 500
        assert ctx.command.cooldown_reset is True

    async def test_robbing_an_empty_wallet_is_refused(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))

        await cog.rob.callback(cog, ctx, FakeUser(id=BOB))

        assert "no money" in ctx.last.lower()
        assert ctx.command.cooldown_reset is True

    async def test_a_successful_robbery_moves_money_between_wallets(self, db, settings, ctx):
        cog = _seeded(Economy(FakeBot(db, settings)), 1)
        await db.add_wallet(BOB, 1_000)

        for _ in range(20):
            await cog.rob.callback(cog, ctx, FakeUser(id=BOB))

        alice = await db.get_account(ALICE)
        bob = await db.get_account(BOB)
        assert alice.wallet + bob.wallet == 1_000
        assert alice.wallet > 0


async def _buy_security(db, settings, user_id: int, levels: int) -> None:
    """Buy ``levels`` of wallet security for a member, funding the bank first."""
    cog = Economy(FakeBot(db, settings))
    ctx = FakeContext(author=FakeUser(id=user_id))
    await db.add_bank(user_id, sum(economy.SECURITY_COST.values()))
    for _ in range(levels):
        await cog.secure.callback(cog, ctx)


class TestWalletSecurity:
    async def test_buying_a_level_charges_the_bank_and_reports_the_new_odds(
        self, db, settings, ctx
    ):
        cog = Economy(FakeBot(db, settings))
        await db.add_bank(ALICE, 10_000)

        await cog.secure.callback(cog, ctx)

        account = await db.get_account(ALICE)
        assert account.security == 1
        assert account.bank == economy.STARTING_BANK + 10_000 - economy.SECURITY_COST[0]
        assert f"{economy.rob_success_percent(1)}%" in ctx.last

    async def test_an_unaffordable_level_is_refused_and_costs_nothing(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))

        await cog.secure.callback(cog, ctx)

        account = await db.get_account(ALICE)
        assert account.security == 0
        assert account.bank == economy.STARTING_BANK
        assert "only have" in ctx.last

    async def test_a_maxed_wallet_cannot_buy_another_level(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_bank(ALICE, sum(economy.SECURITY_COST.values()))
        for _ in economy.SECURITY_COST:
            await cog.secure.callback(cog, ctx)
        before = (await db.get_account(ALICE)).bank

        await cog.secure.callback(cog, ctx)

        account = await db.get_account(ALICE)
        assert account.security == economy.MAX_SECURITY_LEVEL
        assert account.bank == before
        assert "maximum" in ctx.last

    async def test_a_roll_that_robs_an_open_wallet_is_stopped_by_a_defended_one(
        self, db, settings, ctx
    ):
        # A roll of 45 lands under level 0's 50% and over the top level's. The
        # target, the roll, and the wallet are identical in both halves, so the
        # only thing deciding the outcome is the security that was bought.
        await db.add_wallet(BOB, 1_000)
        cog = Economy(FakeBot(db, settings))
        cog.rng = ScriptedRandom(randint=45)

        await cog.rob.callback(cog, ctx, FakeUser(id=BOB))
        assert "robbed" in ctx.last
        assert (await db.get_account(ALICE)).wallet == 45

        await _buy_security(db, settings, BOB, economy.MAX_SECURITY_LEVEL)
        await cog.rob.callback(cog, ctx, FakeUser(id=BOB))

        assert "failed" in ctx.last
        assert (await db.get_account(ALICE)).wallet == 45

    async def test_a_failed_robbery_names_the_defense_that_stopped_it(self, db, settings, ctx):
        await db.add_wallet(BOB, 1_000)
        await _buy_security(db, settings, BOB, 1)
        cog = Economy(FakeBot(db, settings))
        cog.rng = ScriptedRandom(randint=45)

        await cog.rob.callback(cog, ctx, FakeUser(id=BOB))

        assert "security is at level 1" in ctx.last

    async def test_a_failed_robbery_on_an_open_wallet_mentions_no_defense(self, db, settings, ctx):
        await db.add_wallet(BOB, 1_000)
        cog = Economy(FakeBot(db, settings))
        cog.rng = ScriptedRandom(randint=99)

        await cog.rob.callback(cog, ctx, FakeUser(id=BOB))

        assert "failed" in ctx.last
        assert "security" not in ctx.last


class TestMining:
    async def test_mining_without_a_miner_is_refused(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))

        await cog.mine.callback(cog, ctx)

        assert "need to buy" in ctx.last.lower()
        assert ctx.command.cooldown_reset is True

    async def test_the_admin_miner_yields_the_full_amount(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))
        await db.set_miner_level(ALICE, economy.ADMIN_MINER_LEVEL)

        await cog.mine.callback(cog, ctx)

        assert (await db.get_account(ALICE)).crypto == economy.ADMIN_MINE_YIELD

    async def test_a_listed_user_always_mines(self, db, settings, ctx):
        configured = Settings(discord_token="placeholder", always_mine_user_ids=frozenset({ALICE}))
        cog = Mining(FakeBot(db, configured))
        await db.set_miner_level(ALICE, 1)

        for _ in range(5):
            await cog.mine.callback(cog, ctx)

        assert (await db.get_account(ALICE)).crypto == 5

    async def test_a_level_one_miner_sometimes_finds_nothing(self, db, settings, ctx):
        cog = _seeded(Mining(FakeBot(db, settings)), 42)
        await db.set_miner_level(ALICE, 1)

        for _ in range(50):
            await cog.mine.callback(cog, ctx)

        assert any("nothing" in message for message in ctx.sent)


class TestUpgrade:
    async def test_the_first_upgrade_is_affordable_from_the_starting_bank(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))

        await cog.upgrade.callback(cog, ctx)

        account = await db.get_account(ALICE)
        assert account.miner == 1
        assert account.bank == economy.STARTING_BANK - 100
        assert "1%" in ctx.last

    async def test_an_unaffordable_upgrade_names_the_price(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))
        await db.set_miner_level(ALICE, 1)

        await cog.upgrade.callback(cog, ctx)

        assert "$5,000" in ctx.last
        assert (await db.get_account(ALICE)).miner == 1

    async def test_a_maxed_miner_cannot_upgrade(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))
        await db.set_miner_level(ALICE, economy.MAX_MINER_LEVEL)
        await db.add_bank(ALICE, 10_000_000)

        await cog.upgrade.callback(cog, ctx)

        assert "maximum" in ctx.last.lower()
        assert (await db.get_account(ALICE)).miner == economy.MAX_MINER_LEVEL


class TestFlyxcoinMarket:
    async def test_buying_defaults_to_what_you_can_afford(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))
        await db.add_bank(ALICE, 24_000)

        await cog.flx_buy.callback(cog, ctx, None)

        assert (await db.get_account(ALICE)).crypto == 2

    async def test_buying_with_too_little_money_explains_the_price(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))

        await cog.flx_buy.callback(cog, ctx, None)

        assert "$10,000" in ctx.last
        assert (await db.get_account(ALICE)).crypto == 0

    async def test_selling_defaults_to_everything(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))
        await db.add_crypto(ALICE, 3)

        await cog.flx_sell.callback(cog, ctx, None)

        account = await db.get_account(ALICE)
        assert account.crypto == 0
        assert account.bank == economy.STARTING_BANK + 30_000

    async def test_selling_nothing_says_so(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))

        await cog.flx_sell.callback(cog, ctx, None)

        assert "do not have" in ctx.last.lower()

    async def test_sending_to_yourself_is_refused(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))
        await db.add_crypto(ALICE, 5)

        await cog.flx_send.callback(cog, ctx, FakeUser(id=ALICE), 1)

        assert "yourself" in ctx.last.lower()
        assert (await db.get_account(ALICE)).crypto == 5

    async def test_sending_moves_coins(self, db, settings, ctx):
        cog = Mining(FakeBot(db, settings))
        await db.add_crypto(ALICE, 5)

        await cog.flx_send.callback(cog, ctx, FakeUser(id=BOB), 2)

        assert (await db.get_account(ALICE)).crypto == 3
        assert (await db.get_account(BOB)).crypto == 2


class TestCasino:
    async def test_an_invalid_coinflip_guess_costs_nothing(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 100)

        await cog.coinflip.callback(cog, ctx, "edge", 50)

        assert (await db.get_account(ALICE)).wallet == 100
        assert "invalid" in ctx.last.lower()

    async def test_a_coinflip_bet_beyond_the_wallet_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10)

        with pytest.raises(InsufficientFundsError):
            await cog.coinflip.callback(cog, ctx, "heads", 11)

        assert (await db.get_account(ALICE)).wallet == 10

    async def test_a_coinflip_settles_to_one_of_two_outcomes(self, db, settings, ctx):
        cog = _seeded(Gambling(FakeBot(db, settings)), 5)
        await db.add_wallet(ALICE, 100)

        await cog.coinflip.callback(cog, ctx, "heads", 100)

        assert (await db.get_account(ALICE)).wallet in {0, 200}

    async def test_an_rps_tie_goes_to_the_house(self, db, settings, ctx):
        # Refunding the tie is what made RPS pay +33%. The house takes it now,
        # which leaves the game at exactly 0%.
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(choice="rock")
        await db.add_wallet(ALICE, 100)

        await cog.rps.callback(cog, ctx, "rock", 100)

        assert (await db.get_account(ALICE)).wallet == 0
        assert "tie" in ctx.last.lower()

    async def test_an_rps_win_returns_three_times_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(choice="scissors")
        await db.add_wallet(ALICE, 100)

        await cog.rps.callback(cog, ctx, "rock", 100)

        assert (await db.get_account(ALICE)).wallet == 300
        # The message quotes what lands in the wallet, as every other game does.
        assert "$300" in ctx.last

    async def test_an_rps_loss_keeps_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(choice="paper")
        await db.add_wallet(ALICE, 100)

        await cog.rps.callback(cog, ctx, "rock", 100)

        assert (await db.get_account(ALICE)).wallet == 0

    async def test_an_invalid_rps_move_costs_nothing(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 100)

        await cog.rps.callback(cog, ctx, "dynamite", 50)

        assert (await db.get_account(ALICE)).wallet == 100

    async def test_a_dice_win_returns_six_times_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(randint=4)
        await db.add_wallet(ALICE, 100)

        await cog.dice.callback(cog, ctx, 4, 100)

        assert (await db.get_account(ALICE)).wallet == 600

    async def test_a_dice_loss_keeps_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(randint=4)
        await db.add_wallet(ALICE, 100)

        await cog.dice.callback(cog, ctx, 5, 100)

        assert (await db.get_account(ALICE)).wallet == 0

    async def test_an_invalid_roulette_bet_costs_nothing(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 100)

        await cog.roulette.callback(cog, ctx, "37", 50)

        assert (await db.get_account(ALICE)).wallet == 100
        assert "not a valid bet" in ctx.last.lower()

    async def test_a_straight_roulette_win_returns_35_times_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(choice=17)
        await db.add_wallet(ALICE, 100)

        await cog.roulette.callback(cog, ctx, "17", 100)

        assert (await db.get_account(ALICE)).wallet == 3_500

    async def test_a_roulette_color_win_returns_twice_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(choice=1)
        await db.add_wallet(ALICE, 100)

        await cog.roulette.callback(cog, ctx, "red", 100)

        assert (await db.get_account(ALICE)).wallet == 200

    async def test_a_color_bet_loses_on_a_green_pocket(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(choice="00")
        await db.add_wallet(ALICE, 100)

        await cog.roulette.callback(cog, ctx, "red", 100)

        assert (await db.get_account(ALICE)).wallet == 0
        assert "lost" in ctx.text.lower()

    async def test_betting_on_zero_does_not_win_on_double_zero(self, db, settings, ctx):
        # Version 1 collapsed 00 into 0, so a bet on 0 won on both pockets.
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(choice="00")
        await db.add_wallet(ALICE, 100)

        await cog.roulette.callback(cog, ctx, "0", 100)

        assert (await db.get_account(ALICE)).wallet == 0


class TestLeaderboards:
    async def test_the_leaderboard_lists_members(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_wallet(ALICE, 5_000)
        await db.add_wallet(BOB, 100)

        await cog.leaderboard.callback(cog, ctx)

        description = ctx.embeds[0].description
        assert f"<@{ALICE}>" in description
        assert f"<@{BOB}>" in description

    async def test_the_wallet_board_ignores_banked_cash(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_bank(ALICE, 1_000_000)
        await db.add_wallet(BOB, 5)

        await cog.wallets.callback(cog, ctx)

        description = ctx.embeds[0].description
        assert f"<@{BOB}>" in description
        assert f"<@{ALICE}>" not in description

    async def test_balance_reports_every_holding(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await db.add_wallet(ALICE, 250)
        await db.add_crypto(ALICE, 2)

        await cog.balance.callback(cog, ctx, None)

        values = [field.value for field in ctx.embeds[0].fields]
        assert "$250" in values
        assert "2" in values

    async def test_balance_shows_the_wallet_security_level(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))
        await _buy_security(db, settings, ALICE, 2)

        await cog.balance.callback(cog, ctx, None)

        fields = {field.name: field.value for field in ctx.embeds[0].fields}
        assert fields["Wallet Security:"] == "Level 2"

    async def test_security_shares_a_row_with_the_miner_level(self, db, settings, ctx):
        cog = Economy(FakeBot(db, settings))

        await cog.balance.callback(cog, ctx, None)

        # Discord lays consecutive inline fields out as columns of one row, the
        # way Wallet and Bank are paired, and a non-inline field ends the row.
        fields = ctx.embeds[0].fields
        names = [field.name for field in fields]
        miner = names.index("Miner Level:")
        assert names[miner + 1] == "Wallet Security:"
        assert fields[miner].inline is True
        assert fields[miner + 1].inline is True
        assert fields[miner - 1].inline is False


class TestSlots:
    async def test_a_losing_spin_keeps_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cherries = next(s for s in economy.SLOT_REEL if s.name == "cherries")
        lemons = next(s for s in economy.SLOT_REEL if s.name == "lemons")
        grapes = next(s for s in economy.SLOT_REEL if s.name == "grapes")
        cog.rng = CyclingRandom(choices=[cherries, lemons, grapes])
        await db.add_wallet(ALICE, 100)

        await cog.slots.callback(cog, ctx, 100)

        assert (await db.get_account(ALICE)).wallet == 0
        assert "no match" in ctx.text.lower()

    async def test_a_jackpot_pays_the_symbol_rate(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        gems = next(s for s in economy.SLOT_REEL if s.name == "gems")
        cog.rng = ScriptedRandom(choice=gems)
        await db.add_wallet(ALICE, 100)

        await cog.slots.callback(cog, ctx, 100)

        assert (await db.get_account(ALICE)).wallet == 100 * gems.triple_return
        assert "three gems" in ctx.text.lower()

    async def test_a_paying_pair_returns_twice_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        stars = next(s for s in economy.SLOT_REEL if s.name == "stars")
        cherries = next(s for s in economy.SLOT_REEL if s.name == "cherries")
        cog.rng = CyclingRandom(choices=[stars, stars, cherries])
        await db.add_wallet(ALICE, 100)

        await cog.slots.callback(cog, ctx, 100)

        assert (await db.get_account(ALICE)).wallet == 100 * economy.SLOT_PAIR_RETURN
        assert "a pair of stars" in ctx.text.lower()

    async def test_the_reels_are_shown(self, db, settings, ctx):
        cog = _seeded(Gambling(FakeBot(db, settings)), 4)
        await db.add_wallet(ALICE, 100)

        await cog.slots.callback(cog, ctx, 100)

        assert any(symbol.emoji in ctx.text for symbol in economy.SLOT_REEL)

    async def test_a_bet_beyond_the_wallet_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10)

        with pytest.raises(InsufficientFundsError):
            await cog.slots.callback(cog, ctx, 11)

        assert (await db.get_account(ALICE)).wallet == 10

    async def test_the_wallet_never_goes_negative_over_many_spins(self, db, settings, ctx):
        cog = _seeded(Gambling(FakeBot(db, settings)), 11)
        await db.add_wallet(ALICE, 1_000)

        for _ in range(100):
            balance = (await db.get_account(ALICE)).wallet
            if balance < 10:
                break
            await cog.slots.callback(cog, ctx, 10)
            assert (await db.get_account(ALICE)).wallet >= 0


class TestWar:
    async def test_a_higher_card_wins_double(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(sample=[economy.Card(14, "♠"), economy.Card(5, "♥")])
        await db.add_wallet(ALICE, 100)

        await cog.war.callback(cog, ctx, 100)

        assert (await db.get_account(ALICE)).wallet == 200
        assert "you win" in ctx.text.lower()

    async def test_a_lower_card_loses_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(sample=[economy.Card(3, "♠"), economy.Card(12, "♥")])
        await db.add_wallet(ALICE, 100)

        await cog.war.callback(cog, ctx, 100)

        assert (await db.get_account(ALICE)).wallet == 0
        assert "you lose" in ctx.text.lower()

    async def test_a_tie_returns_the_stake(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(sample=[economy.Card(9, "♠"), economy.Card(9, "♦")])
        await db.add_wallet(ALICE, 100)

        await cog.war.callback(cog, ctx, 100)

        assert (await db.get_account(ALICE)).wallet == 100
        assert "tie" in ctx.text.lower()

    async def test_both_cards_are_shown(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        cog.rng = ScriptedRandom(sample=[economy.Card(14, "♠"), economy.Card(2, "♥")])
        await db.add_wallet(ALICE, 100)

        await cog.war.callback(cog, ctx, 100)

        assert "A♠" in ctx.text
        assert "2♥" in ctx.text

    async def test_a_bet_beyond_the_wallet_is_refused(self, db, settings, ctx):
        cog = Gambling(FakeBot(db, settings))
        await db.add_wallet(ALICE, 10)

        with pytest.raises(InsufficientFundsError):
            await cog.war.callback(cog, ctx, 11)

        assert (await db.get_account(ALICE)).wallet == 10
