"""Tests for the persistence layer."""

from __future__ import annotations

import asyncio

import pytest

from flyconomy import economy
from flyconomy.database import Database
from flyconomy.errors import InsufficientFundsError
from tests.conftest import ALICE, BOB, CAROL


class TestAccounts:
    async def test_a_new_member_starts_with_the_documented_balances(self, db: Database):
        account = await db.get_account(ALICE)
        assert account.wallet == economy.STARTING_WALLET
        assert account.bank == economy.STARTING_BANK
        assert account.crypto == 0
        assert account.miner == 0

    async def test_find_account_does_not_create_one(self, db: Database):
        assert await db.find_account(ALICE) is None

    async def test_get_account_is_idempotent(self, db: Database):
        first = await db.get_account(ALICE)
        await db.add_wallet(ALICE, 500)
        second = await db.get_account(ALICE)
        assert second.wallet == first.wallet + 500

    async def test_ensure_account_never_overwrites_a_balance(self, db: Database):
        await db.add_bank(ALICE, 10_000)
        await db.ensure_account(ALICE)
        account = await db.get_account(ALICE)
        assert account.bank == economy.STARTING_BANK + 10_000

    async def test_net_worth_includes_flyxcoin(self, db: Database):
        await db.add_crypto(ALICE, 3)
        account = await db.get_account(ALICE)
        assert account.net_worth == economy.STARTING_BANK + 3 * economy.FLX_PRICE

    async def test_delete_account_removes_the_row(self, db: Database):
        await db.get_account(ALICE)
        assert await db.delete_account(ALICE) is True
        assert await db.find_account(ALICE) is None

    async def test_deleting_a_missing_account_reports_nothing_removed(self, db: Database):
        assert await db.delete_account(ALICE) is False

    async def test_purge_removes_the_account_and_the_lottery_entry(self, db: Database):
        await db.add_bank(ALICE, 5_000)
        await db.enter_lottery(ALICE, 1_000)

        result = await db.purge_user(ALICE)

        assert result.account is True
        assert result.lottery_entries == 1
        assert result.found is True
        assert await db.find_account(ALICE) is None
        assert await db.lottery_entrants() == []

    async def test_purge_reports_an_id_that_was_never_in_the_database(self, db: Database):
        result = await db.purge_user(ALICE)

        assert result.account is False
        assert result.lottery_entries == 0
        assert result.found is False

    async def test_purge_removes_an_entry_left_without_an_account(self, db: Database):
        await db.add_bank(ALICE, 5_000)
        await db.enter_lottery(ALICE, 1_000)
        # A ticket with no account behind it, which is how a member deleted
        # before purge_user existed would look.
        await db._db.execute("DELETE FROM bank WHERE user = ?", (ALICE,))

        result = await db.purge_user(ALICE)

        assert result.account is False
        assert result.lottery_entries == 1
        assert result.found is True

    async def test_purge_leaves_other_members_alone(self, db: Database):
        await db.add_bank(BOB, 5_000)
        await db.enter_lottery(BOB, 1_000)

        await db.purge_user(ALICE)

        assert await db.find_account(BOB) is not None
        assert await db.lottery_entrants() == [BOB]

    async def test_purge_accepts_an_id_no_real_member_could_own(self, db: Database):
        await db.add_wallet(1, 5)

        assert (await db.purge_user(1)).account is True
        assert await db.find_account(1) is None

    async def test_resetting_a_member_also_clears_their_lottery_entry(self, db: Database):
        await db.add_bank(ALICE, 5_000)
        await db.enter_lottery(ALICE, 1_000)

        assert await db.delete_account(ALICE) is True
        assert await db.lottery_entrants() == []


class TestBalanceChanges:
    async def test_credit_and_debit(self, db: Database):
        await db.add_wallet(ALICE, 250)
        await db.add_wallet(ALICE, -100)
        assert (await db.get_account(ALICE)).wallet == 150

    async def test_a_debit_below_zero_is_refused(self, db: Database):
        with pytest.raises(InsufficientFundsError):
            await db.add_wallet(ALICE, -1)

    async def test_a_refused_debit_leaves_the_balance_untouched(self, db: Database):
        await db.add_wallet(ALICE, 100)
        with pytest.raises(InsufficientFundsError):
            await db.add_wallet(ALICE, -101)
        assert (await db.get_account(ALICE)).wallet == 100

    async def test_the_error_reports_the_shortfall(self, db: Database):
        await db.add_wallet(ALICE, 40)
        with pytest.raises(InsufficientFundsError) as caught:
            await db.add_wallet(ALICE, -100)
        assert caught.value.available == 40
        assert caught.value.requested == 100

    async def test_crypto_cannot_go_negative(self, db: Database):
        with pytest.raises(InsufficientFundsError) as caught:
            await db.add_crypto(ALICE, -1)
        assert caught.value.currency == "Flyxcoin"

    async def test_an_unknown_column_is_rejected(self, db: Database):
        with pytest.raises(ValueError, match="unsupported balance column"):
            await db._adjust(ALICE, column="miner", amount=1, currency="levels")

    async def test_concurrent_credits_do_not_lose_updates(self, db: Database):
        # The original bot read a balance into Python and wrote it back, so
        # interleaved commands overwrote each other. These run as relative SQL
        # updates, so every one of them lands.
        await asyncio.gather(*(db.add_wallet(ALICE, 10) for _ in range(50)))
        assert (await db.get_account(ALICE)).wallet == 500

    async def test_concurrent_debits_cannot_overdraw(self, db: Database):
        await db.add_wallet(ALICE, 100)
        results = await asyncio.gather(
            *(db.add_wallet(ALICE, -30) for _ in range(10)),
            return_exceptions=True,
        )
        succeeded = sum(1 for result in results if result is None)
        assert succeeded == 3
        assert (await db.get_account(ALICE)).wallet == 10


class TestTransfers:
    async def test_deposit_moves_cash_from_wallet_to_bank(self, db: Database):
        await db.add_wallet(ALICE, 500)
        await db.transfer(ALICE, source="wallet", destination="bank", amount=200)
        account = await db.get_account(ALICE)
        assert account.wallet == 300
        assert account.bank == economy.STARTING_BANK + 200

    async def test_withdraw_moves_cash_from_bank_to_wallet(self, db: Database):
        await db.transfer(ALICE, source="bank", destination="wallet", amount=1_000)
        account = await db.get_account(ALICE)
        assert account.wallet == 1_000
        assert account.bank == 0

    async def test_a_transfer_preserves_total_cash(self, db: Database):
        await db.add_wallet(ALICE, 777)
        before = await db.get_account(ALICE)
        await db.transfer(ALICE, source="wallet", destination="bank", amount=500)
        after = await db.get_account(ALICE)
        assert before.wallet + before.bank == after.wallet + after.bank

    async def test_an_oversized_transfer_is_refused_atomically(self, db: Database):
        before = await db.get_account(ALICE)
        with pytest.raises(InsufficientFundsError):
            await db.transfer(ALICE, source="wallet", destination="bank", amount=1)
        assert await db.get_account(ALICE) == before

    async def test_a_negative_transfer_is_rejected(self, db: Database):
        with pytest.raises(ValueError, match="must not be negative"):
            await db.transfer(ALICE, source="wallet", destination="bank", amount=-5)

    async def test_a_non_cash_column_is_rejected(self, db: Database):
        with pytest.raises(ValueError, match="unsupported cash columns"):
            await db.transfer(ALICE, source="crypto", destination="bank", amount=1)


class TestCryptoMarket:
    async def test_buying_charges_the_bank_and_credits_coins(self, db: Database):
        await db.add_bank(ALICE, 30_000)
        cost = await db.buy_crypto(ALICE, 3)
        account = await db.get_account(ALICE)
        assert cost == 3 * economy.FLX_PRICE
        assert account.crypto == 3
        assert account.bank == economy.STARTING_BANK

    async def test_buying_more_than_you_can_afford_is_refused(self, db: Database):
        with pytest.raises(InsufficientFundsError):
            await db.buy_crypto(ALICE, 1)
        assert (await db.get_account(ALICE)).crypto == 0

    async def test_selling_credits_the_bank(self, db: Database):
        await db.add_crypto(ALICE, 2)
        proceeds = await db.sell_crypto(ALICE, 2)
        account = await db.get_account(ALICE)
        assert proceeds == 2 * economy.FLX_PRICE
        assert account.crypto == 0
        assert account.bank == economy.STARTING_BANK + proceeds

    async def test_selling_coins_you_lack_is_refused(self, db: Database):
        await db.add_crypto(ALICE, 1)
        with pytest.raises(InsufficientFundsError) as caught:
            await db.sell_crypto(ALICE, 2)
        assert caught.value.currency == "Flyxcoin"
        assert (await db.get_account(ALICE)).crypto == 1

    async def test_buying_then_selling_returns_the_original_bank_balance(self, db: Database):
        await db.add_bank(ALICE, 50_000)
        before = (await db.get_account(ALICE)).bank
        await db.buy_crypto(ALICE, 5)
        await db.sell_crypto(ALICE, 5)
        assert (await db.get_account(ALICE)).bank == before

    @pytest.mark.parametrize("amount", [0, -1])
    async def test_non_positive_trades_are_rejected(self, db: Database, amount):
        with pytest.raises(ValueError, match="must be positive"):
            await db.buy_crypto(ALICE, amount)
        with pytest.raises(ValueError, match="must be positive"):
            await db.sell_crypto(ALICE, amount)

    async def test_total_crypto_sums_every_holder(self, db: Database):
        await db.add_crypto(ALICE, 4)
        await db.add_crypto(BOB, 6)
        await db.get_account(CAROL)
        assert await db.total_crypto() == 10

    async def test_total_crypto_is_zero_on_an_empty_database(self, db: Database):
        assert await db.total_crypto() == 0


class TestLiveFlxPrice:
    async def test_a_new_database_starts_at_the_base_price(self, db: Database):
        assert await db.get_flx_price() == economy.FLX_PRICE

    async def test_set_flx_price_updates_the_live_price(self, db: Database):
        await db.set_flx_price(7_500)
        assert await db.get_flx_price() == 7_500

    async def test_a_non_positive_price_is_rejected(self, db: Database):
        with pytest.raises(ValueError, match="must be positive"):
            await db.set_flx_price(0)

    async def test_buying_charges_the_live_price(self, db: Database):
        await db.set_flx_price(5_000)
        await db.add_bank(ALICE, 15_000)
        cost = await db.buy_crypto(ALICE, 3)
        assert cost == 15_000
        assert (await db.get_account(ALICE)).crypto == 3

    async def test_selling_credits_the_live_price(self, db: Database):
        await db.set_flx_price(20_000)
        await db.add_crypto(ALICE, 2)
        proceeds = await db.sell_crypto(ALICE, 2)
        assert proceeds == 40_000

    async def test_get_account_reports_the_price_it_was_read_at(self, db: Database):
        await db.set_flx_price(6_000)
        assert (await db.get_account(ALICE)).flx_price == 6_000

    async def test_net_worth_reflects_the_live_price(self, db: Database):
        await db.add_crypto(ALICE, 2)
        await db.set_flx_price(6_000)
        account = await db.get_account(ALICE)
        assert account.net_worth == economy.STARTING_BANK + 12_000


class TestPeerTransfers:
    async def test_sending_coins_moves_them(self, db: Database):
        await db.add_crypto(ALICE, 5)
        await db.transfer_crypto(ALICE, BOB, 2)
        assert (await db.get_account(ALICE)).crypto == 3
        assert (await db.get_account(BOB)).crypto == 2

    async def test_sending_coins_preserves_the_total_supply(self, db: Database):
        await db.add_crypto(ALICE, 5)
        await db.transfer_crypto(ALICE, BOB, 5)
        assert await db.total_crypto() == 5

    async def test_sending_more_than_you_hold_moves_nothing(self, db: Database):
        await db.add_crypto(ALICE, 1)
        with pytest.raises(InsufficientFundsError):
            await db.transfer_crypto(ALICE, BOB, 2)
        assert (await db.get_account(ALICE)).crypto == 1
        assert await db.find_account(BOB) is None or (await db.get_account(BOB)).crypto == 0

    async def test_stealing_moves_wallet_cash(self, db: Database):
        await db.add_wallet(BOB, 400)
        await db.steal(ALICE, BOB, 150)
        assert (await db.get_account(ALICE)).wallet == 150
        assert (await db.get_account(BOB)).wallet == 250

    async def test_stealing_more_than_the_victim_has_is_refused(self, db: Database):
        await db.add_wallet(BOB, 10)
        with pytest.raises(InsufficientFundsError):
            await db.steal(ALICE, BOB, 11)
        assert (await db.get_account(BOB)).wallet == 10
        assert (await db.get_account(ALICE)).wallet == 0

    @pytest.mark.parametrize("amount", [0, -5])
    async def test_non_positive_amounts_are_rejected(self, db: Database, amount):
        with pytest.raises(ValueError, match="must be positive"):
            await db.steal(ALICE, BOB, amount)
        with pytest.raises(ValueError, match="must be positive"):
            await db.transfer_crypto(ALICE, BOB, amount)


class TestMinerUpgrades:
    async def test_upgrading_charges_the_bank_and_raises_the_level(self, db: Database):
        level = await db.buy_miner_upgrade(ALICE, 100)
        account = await db.get_account(ALICE)
        assert level == 1
        assert account.miner == 1
        assert account.bank == economy.STARTING_BANK - 100

    async def test_an_unaffordable_upgrade_changes_nothing(self, db: Database):
        with pytest.raises(InsufficientFundsError):
            await db.buy_miner_upgrade(ALICE, 5_000)
        account = await db.get_account(ALICE)
        assert account.miner == 0
        assert account.bank == economy.STARTING_BANK

    async def test_set_miner_level_overrides_the_level(self, db: Database):
        await db.set_miner_level(ALICE, economy.ADMIN_MINER_LEVEL)
        assert (await db.get_account(ALICE)).miner == economy.ADMIN_MINER_LEVEL


class TestLeaderboards:
    async def test_net_worth_ranking_is_highest_first(self, db: Database):
        await db.add_wallet(ALICE, 100)
        await db.add_wallet(BOB, 5_000)
        await db.add_crypto(CAROL, 1)

        entries = await db.top_net_worth()
        assert [entry.user_id for entry in entries] == [CAROL, BOB, ALICE]

    async def test_net_worth_ranking_counts_coins_at_the_flyxcoin_price(self, db: Database):
        await db.add_crypto(ALICE, 2)
        entries = await db.top_net_worth()
        assert entries[0].amount == economy.STARTING_BANK + 2 * economy.FLX_PRICE

    async def test_net_worth_ranking_uses_the_live_price(self, db: Database):
        await db.add_crypto(ALICE, 2)
        await db.set_flx_price(6_000)
        entries = await db.top_net_worth()
        assert entries[0].amount == economy.STARTING_BANK + 12_000

    async def test_wallet_ranking_ignores_banked_cash(self, db: Database):
        await db.add_bank(ALICE, 1_000_000)
        await db.add_wallet(BOB, 5)

        entries = await db.top_wallets()
        assert [entry.user_id for entry in entries] == [BOB]

    async def test_rankings_are_capped(self, db: Database):
        for index in range(15):
            await db.add_wallet(ALICE + index, index + 1)
        assert len(await db.top_wallets()) == economy.LEADERBOARD_SIZE

    async def test_a_custom_limit_is_honored(self, db: Database):
        for index in range(5):
            await db.add_wallet(ALICE + index, index + 1)
        assert len(await db.top_wallets(limit=2)) == 2

    async def test_ties_break_consistently(self, db: Database):
        await db.add_wallet(BOB, 100)
        await db.add_wallet(ALICE, 100)
        first = await db.top_wallets()
        second = await db.top_wallets()
        assert [entry.user_id for entry in first] == [entry.user_id for entry in second]

    async def test_empty_rankings_are_empty_lists(self, db: Database):
        assert await db.top_net_worth() == []
        assert await db.top_wallets() == []


class TestLifecycle:
    async def test_the_database_works_as_a_context_manager(self, db_path):
        async with await Database.connect(db_path) as database:
            await database.get_account(ALICE)
        reopened = await Database.connect(db_path)
        try:
            assert (await reopened.find_account(ALICE)) is not None
        finally:
            await reopened.close()

    async def test_the_parent_directory_is_created(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "bot.db"
        database = await Database.connect(path)
        await database.close()
        assert path.exists()

    async def test_reads_use_a_connection_separate_from_writes(self, db: Database):
        # A read sharing the writer's connection would queue behind whatever
        # that connection's single background thread is doing -- a write, or
        # an occasional WAL checkpoint -- no matter how fast the read itself
        # is. A dedicated connection is what lets it skip that queue.
        assert db._reader is not db._db  # type: ignore[attr-defined]

    async def test_closing_closes_the_reader_connection_too(self, db_path):
        database = await Database.connect(db_path)
        reader = database._reader  # type: ignore[attr-defined]
        await database.close()
        with pytest.raises(ValueError):
            await reader.execute("SELECT 1")
