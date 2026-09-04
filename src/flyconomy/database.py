"""Persistence layer.

The bot stores every member's balances in a single SQLite table named ``bank``.
That table, including its column names and order, is inherited from version 1 of
the bot and is preserved exactly so an existing ``bot.db`` keeps working. Schema
changes are applied as numbered migrations tracked in SQLite's built-in
``user_version`` pragma.

All balance changes are expressed as relative SQL updates (``SET wallet = wallet
+ ?``) rather than a read in Python followed by a write. This makes each change
atomic, so two commands running concurrently for the same member cannot lose an
update the way the original read-modify-write helpers could.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self

import aiosqlite

from flyconomy import economy, jackpot
from flyconomy.errors import InsufficientFundsError

log = logging.getLogger(__name__)

#: Schema version this build expects. Bump it and add a migration below.
SCHEMA_VERSION: Final = 5

#: Balance columns that may be adjusted. Values are interpolated into SQL, so
#: every caller is checked against this set first.
_BALANCE_COLUMNS: Final[frozenset[str]] = frozenset({"wallet", "bank", "crypto"})

#: Cash columns that money can be moved between.
_CASH_COLUMNS: Final[frozenset[str]] = frozenset({"wallet", "bank"})


@dataclass(frozen=True, slots=True)
class Account:
    """A member's balances.

    Attributes:
        user_id: The member's Discord snowflake.
        wallet: Undeposited cash, which other members can steal with ``rob``.
        bank: Deposited cash, which is safe from theft.
        crypto: Flyxcoin held.
        miner: Miner level, where ``0`` means the member owns no miner.
        flx_price: The live Flyxcoin price at the moment this account was read.
    """

    user_id: int
    wallet: int
    bank: int
    crypto: int
    miner: int
    flx_price: int

    @property
    def net_worth(self) -> int:
        """Total value of the account in dollars, at the price it was read."""
        return economy.net_worth(self.wallet, self.bank, self.crypto, self.flx_price)


@dataclass(frozen=True, slots=True)
class LotteryState:
    """The lottery as it currently stands.

    Attributes:
        pot: Dollars waiting to be won.
        draw: The draw now open for entries, counting from one.
        entrants: How many members have entered the open draw.
    """

    pot: int
    draw: int
    entrants: int


@dataclass(frozen=True, slots=True)
class JackpotState:
    """The open jackpot round as it currently stands.

    The pot is derived from the entries rather than stored beside them, so the
    two can never drift apart: every dollar in the pot is one an entrant is
    still recorded as having anted.

    Attributes:
        round_number: The round now accepting antes, counting from one.
        entries: Everyone in the round, in the order they entered.
    """

    round_number: int
    entries: tuple[jackpot.Entry, ...]

    @property
    def pot(self) -> int:
        """Dollars anted into the round so far."""
        return jackpot.total_pot(self.entries)

    @property
    def entrants(self) -> int:
        """How many members are in the round."""
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """One row of a ranked listing.

    Attributes:
        user_id: The member's Discord snowflake.
        amount: The ranked value in dollars.
    """

    user_id: int
    amount: int


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """What removing a member from the database actually deleted.

    Attributes:
        account: Whether a ``bank`` row was removed.
        lottery_entries: How many open-draw entries were removed.
        jackpot_entries: How many open-round jackpot antes were removed.
    """

    account: bool
    lottery_entries: int
    jackpot_entries: int = 0

    @property
    def found(self) -> bool:
        """Whether the member existed anywhere in the database."""
        return self.account or self.lottery_entries > 0 or self.jackpot_entries > 0


class Database:
    """An async wrapper around the bot's SQLite database.

    Instances are created with :meth:`connect` and closed with :meth:`close`, or
    used as an async context manager.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        """Wrap an open connection. Prefer :meth:`connect`."""
        self._db = connection
        # Falls back to the write connection until connect() attaches a
        # dedicated one; only connect() actually gives reads their own thread.
        self._reader = connection
        # A single connection cannot interleave transactions, so multi-statement
        # operations take this lock to serialize themselves.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle --

    @classmethod
    async def connect(cls, path: Path) -> Self:
        """Open the database, applying any pending migrations.

        Args:
            path: Location of the SQLite file. Parent directories are created.

        Returns:
            A ready-to-use database.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None puts the driver in autocommit mode, which lets
        # _transaction() control transaction boundaries explicitly.
        connection = await aiosqlite.connect(path, isolation_level=None)
        connection.row_factory = aiosqlite.Row

        # WAL lets reads proceed during a write, and NORMAL is the durability
        # level WAL is designed for.
        await connection.execute("PRAGMA journal_mode = WAL")
        await connection.execute("PRAGMA synchronous = NORMAL")
        await connection.execute("PRAGMA foreign_keys = ON")

        self = cls(connection)
        try:
            await self.migrate()
        except BaseException:
            # A half-open connection would keep the file locked and leak a
            # thread, so fail cleanly instead.
            await connection.close()
            raise

        # aiosqlite runs every statement on one connection's own dedicated
        # background thread, in the order it was queued. A read sharing the
        # writer's connection would sit behind whatever write (or WAL
        # checkpoint) is already running there, no matter how fast the read
        # itself is. WAL mode is built for concurrent readers, so a second
        # connection is what actually lets a read skip that queue.
        reader = await aiosqlite.connect(path, isolation_level=None)
        reader.row_factory = aiosqlite.Row
        await reader.execute("PRAGMA query_only = ON")
        self._reader = reader
        return self

    async def close(self) -> None:
        """Close the underlying connections."""
        if self._reader is not self._db:
            await self._reader.close()
        await self._db.close()

    async def __aenter__(self) -> Self:
        """Enter the context manager."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the database on exit."""
        await self.close()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a block inside one immediate transaction, rolling back on error."""
        async with self._lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                await self._db.execute("ROLLBACK")
                raise
            await self._db.execute("COMMIT")

    # ------------------------------------------------------------ migration --

    async def migrate(self) -> None:
        """Bring the schema up to :data:`SCHEMA_VERSION`.

        Migrations are idempotent and forward-only. A database created by
        version 1 of the bot starts at ``user_version = 0`` and is upgraded in
        place without losing rows.

        Raises:
            RuntimeError: If the database was written by a newer build.
        """
        async with self._db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        current: int = row[0] if row else 0

        if current > SCHEMA_VERSION:
            msg = (
                f"database schema version {current} is newer than this build "
                f"supports ({SCHEMA_VERSION}); upgrade the bot"
            )
            raise RuntimeError(msg)
        if current == SCHEMA_VERSION:
            log.debug("Schema is up to date at version %d", current)
            return

        log.info("Migrating schema from version %d to %d", current, SCHEMA_VERSION)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            await _MIGRATIONS[version](self._db)
            await self._db.execute(f"PRAGMA user_version = {version}")
            log.info("Applied migration %d", version)

    # ---------------------------------------------------------------- reads --

    async def get_account(self, user_id: int) -> Account:
        """Return a member's account, creating it with starting funds if new.

        Args:
            user_id: The member's Discord snowflake.

        Returns:
            The member's current balances.

        Raises:
            RuntimeError: If the account disappears between creation and read.
        """
        account = await self.find_account(user_id)
        if account is not None:
            return account

        await self.ensure_account(user_id)
        created = await self.find_account(user_id)
        if created is None:  # pragma: no cover - only on a concurrent DELETE
            msg = f"account {user_id} vanished immediately after creation"
            raise RuntimeError(msg)
        return created

    async def find_account(self, user_id: int) -> Account | None:
        """Return a member's account, or ``None`` when they have never played."""
        async with self._reader.execute(
            "SELECT b.wallet, b.bank, b.crypto, b.miner, m.price AS flx_price "
            "FROM bank b, market m WHERE b.user = ? AND m.id = 1",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return Account(
            user_id=user_id,
            wallet=row["wallet"],
            bank=row["bank"],
            crypto=row["crypto"],
            miner=row["miner"],
            flx_price=row["flx_price"],
        )

    async def ensure_account(self, user_id: int) -> None:
        """Create an account with starting funds if the member has none."""
        await self._db.execute(
            "INSERT INTO bank (wallet, bank, crypto, miner, user) "
            "VALUES (?, ?, 0, 0, ?) ON CONFLICT(user) DO NOTHING",
            (economy.STARTING_WALLET, economy.STARTING_BANK, user_id),
        )

    async def total_crypto(self) -> int:
        """Return the number of Flyxcoin in circulation across all accounts."""
        async with self._reader.execute(
            "SELECT COALESCE(SUM(crypto), 0) AS total FROM bank WHERE crypto > 0"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    async def top_net_worth(self, limit: int = economy.LEADERBOARD_SIZE) -> list[LeaderboardEntry]:
        """Return the richest members by net worth, highest first."""
        price = await self.get_flx_price()
        return await self._ranked(f"wallet + bank + (crypto * {price})", limit=limit)

    async def get_flx_price(self) -> int:
        """Return the live Flyxcoin price."""
        async with self._reader.execute("SELECT price FROM market WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        if row is None:  # pragma: no cover - migration 4 guarantees the row
            return economy.FLX_PRICE
        return int(row["price"])

    async def set_flx_price(self, price: int) -> None:
        """Set the live Flyxcoin price. Used by the scheduled market tick.

        Args:
            price: The new price. Must be positive.

        Raises:
            ValueError: If ``price`` is not positive.
        """
        if price <= 0:
            msg = "price must be positive"
            raise ValueError(msg)
        async with self._transaction() as db:
            await db.execute("UPDATE market SET price = ? WHERE id = 1", (price,))

    @staticmethod
    async def _read_flx_price(db: aiosqlite.Connection) -> int:
        """Read the live Flyxcoin price for use inside an already-open transaction.

        Args:
            db: Connection to read on, so the read joins the open transaction
                and a trade always charges whatever price it actually reads.

        Returns:
            The current price.
        """
        async with db.execute("SELECT price FROM market WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        if row is None:  # pragma: no cover - migration 4 guarantees the row
            return economy.FLX_PRICE
        return int(row["price"])

    async def top_wallets(self, limit: int = economy.LEADERBOARD_SIZE) -> list[LeaderboardEntry]:
        """Return the largest undeposited wallets, highest first."""
        return await self._ranked("wallet", limit=limit)

    async def _ranked(self, expression: str, limit: int) -> list[LeaderboardEntry]:
        """Return the top ``limit`` accounts by a SQL ``expression``.

        Args:
            expression: A SQL expression over the ``bank`` columns. It is
                interpolated into the query, so it must never be built from
                user input. Call sites pass a module-level constant or a
                previously-read integer, never raw text.
            limit: Maximum rows to return.

        Returns:
            Ranked entries with a positive amount, highest first. Ties break on
            user ID so the ordering is stable between calls.
        """
        # `expression` is a trusted module-level constant, never user input.
        query = (
            f"SELECT user, {expression} AS amount FROM bank "  # noqa: S608
            f"WHERE {expression} > 0 ORDER BY amount DESC, user ASC LIMIT ?"
        )
        async with self._reader.execute(query, (limit,)) as cursor:
            rows = await cursor.fetchall()
        return [LeaderboardEntry(user_id=row["user"], amount=row["amount"]) for row in rows]

    # --------------------------------------------------------------- writes --

    async def add_wallet(self, user_id: int, amount: int) -> None:
        """Add ``amount`` dollars to a wallet. A negative amount debits it.

        Args:
            user_id: The member's Discord snowflake.
            amount: Signed dollar change.

        Raises:
            InsufficientFundsError: If the change would overdraw the wallet.
        """
        await self._adjust(user_id, column="wallet", amount=amount, currency="funds")

    async def add_bank(self, user_id: int, amount: int) -> None:
        """Add ``amount`` dollars to a bank balance. A negative amount debits it.

        Args:
            user_id: The member's Discord snowflake.
            amount: Signed dollar change.

        Raises:
            InsufficientFundsError: If the change would overdraw the bank balance.
        """
        await self._adjust(user_id, column="bank", amount=amount, currency="funds")

    async def add_crypto(self, user_id: int, amount: int) -> None:
        """Add ``amount`` Flyxcoin. A negative amount debits them.

        Args:
            user_id: The member's Discord snowflake.
            amount: Signed coin change.

        Raises:
            InsufficientFundsError: If the change would leave a negative balance.
        """
        await self._adjust(user_id, column="crypto", amount=amount, currency="Flyxcoin")

    async def _adjust(self, user_id: int, *, column: str, amount: int, currency: str) -> None:
        """Apply a relative change to one balance column.

        The non-negative guard is part of the UPDATE statement, so a concurrent
        command cannot slip a debit past a balance check.

        Args:
            user_id: The member's Discord snowflake.
            column: One of ``wallet``, ``bank``, or ``crypto``.
            amount: Signed change to apply.
            currency: Name used in the error message when the change is refused.

        Raises:
            InsufficientFundsError: If the change would leave a negative balance.
            ValueError: If ``column`` is not a balance column.
        """
        if column not in _BALANCE_COLUMNS:
            msg = f"unsupported balance column: {column!r}"
            raise ValueError(msg)

        async with self._transaction() as db:
            await self.ensure_account(user_id)
            cursor = await db.execute(
                # `column` is validated against _BALANCE_COLUMNS above.
                f"UPDATE bank SET {column} = {column} + ? WHERE user = ? AND {column} + ? >= 0",  # noqa: S608
                (amount, user_id, amount),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(
                    await self._read_column(db, user_id, column), abs(amount), currency
                )

    @staticmethod
    async def _read_column(db: aiosqlite.Connection, user_id: int, column: str) -> int:
        """Read one already-validated balance column, defaulting to zero.

        Args:
            db: Connection to read on, so the read joins the open transaction.
            user_id: The member's Discord snowflake.
            column: A column name that the caller has already validated.

        Returns:
            The current balance, or ``0`` when the account is missing.
        """
        async with db.execute(
            # `column` is validated by the caller against _BALANCE_COLUMNS.
            f"SELECT {column} AS balance FROM bank WHERE user = ?",  # noqa: S608
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["balance"]) if row else 0

    async def transfer(self, user_id: int, *, source: str, destination: str, amount: int) -> None:
        """Move money between a member's own wallet and bank.

        Args:
            user_id: The member's Discord snowflake.
            source: Column to debit, ``"wallet"`` or ``"bank"``.
            destination: Column to credit, ``"wallet"`` or ``"bank"``.
            amount: Dollars to move. Must not be negative.

        Raises:
            InsufficientFundsError: If the source balance is too small.
            ValueError: If ``amount`` is negative or a column is not cash.
        """
        if amount < 0:
            msg = "transfer amount must not be negative"
            raise ValueError(msg)
        if not {source, destination} <= _CASH_COLUMNS:
            msg = f"unsupported cash columns: {source!r} -> {destination!r}"
            raise ValueError(msg)

        async with self._transaction() as db:
            await self.ensure_account(user_id)
            cursor = await db.execute(
                # Both columns are validated against _CASH_COLUMNS above.
                f"UPDATE bank SET {source} = {source} - ?, {destination} = {destination} + ? "  # noqa: S608
                f"WHERE user = ? AND {source} >= ?",
                (amount, amount, user_id, amount),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(await self._read_column(db, user_id, source), amount)

    async def transfer_crypto(self, sender_id: int, recipient_id: int, amount: int) -> None:
        """Move Flyxcoin from one member to another.

        Args:
            sender_id: Member sending the coins.
            recipient_id: Member receiving them.
            amount: Coins to send. Must be positive.

        Raises:
            InsufficientFundsError: If the sender holds too few coins.
            ValueError: If ``amount`` is not positive.
        """
        if amount <= 0:
            msg = "transfer amount must be positive"
            raise ValueError(msg)

        async with self._transaction() as db:
            await self.ensure_account(sender_id)
            await self.ensure_account(recipient_id)
            cursor = await db.execute(
                "UPDATE bank SET crypto = crypto - ? WHERE user = ? AND crypto >= ?",
                (amount, sender_id, amount),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(
                    await self._read_column(db, sender_id, "crypto"), amount, "Flyxcoin"
                )
            await db.execute(
                "UPDATE bank SET crypto = crypto + ? WHERE user = ?", (amount, recipient_id)
            )

    async def steal(self, thief_id: int, victim_id: int, amount: int) -> None:
        """Move wallet cash from a victim to a thief in one transaction.

        Args:
            thief_id: Member receiving the cash.
            victim_id: Member losing it.
            amount: Dollars to move. Must be positive.

        Raises:
            InsufficientFundsError: If the victim's wallet is too small.
            ValueError: If ``amount`` is not positive.
        """
        if amount <= 0:
            msg = "stolen amount must be positive"
            raise ValueError(msg)

        async with self._transaction() as db:
            await self.ensure_account(thief_id)
            await self.ensure_account(victim_id)
            cursor = await db.execute(
                "UPDATE bank SET wallet = wallet - ? WHERE user = ? AND wallet >= ?",
                (amount, victim_id, amount),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(
                    await self._read_column(db, victim_id, "wallet"), amount
                )
            await db.execute(
                "UPDATE bank SET wallet = wallet + ? WHERE user = ?", (amount, thief_id)
            )

    async def buy_miner_upgrade(self, user_id: int, cost: int) -> int:
        """Charge a member's bank balance and raise their miner one level.

        Args:
            user_id: The member's Discord snowflake.
            cost: Dollars to charge against the bank balance.

        Returns:
            The new miner level.

        Raises:
            InsufficientFundsError: If the bank balance is too small.
        """
        async with self._transaction() as db:
            await self.ensure_account(user_id)
            cursor = await db.execute(
                "UPDATE bank SET bank = bank - ?, miner = miner + 1 WHERE user = ? AND bank >= ?",
                (cost, user_id, cost),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(await self._read_column(db, user_id, "bank"), cost)
            return await self._read_column(db, user_id, "miner")

    async def buy_crypto(self, user_id: int, amount: int) -> int:
        """Exchange bank dollars for Flyxcoin at the live market price.

        Args:
            user_id: The member's Discord snowflake.
            amount: Coins to buy. Must be positive.

        Returns:
            The dollar cost that was charged.

        Raises:
            InsufficientFundsError: If the bank balance is too small.
            ValueError: If ``amount`` is not positive.
        """
        if amount <= 0:
            msg = "purchase amount must be positive"
            raise ValueError(msg)

        async with self._transaction() as db:
            await self.ensure_account(user_id)
            cost = economy.flx_cost(amount, await self._read_flx_price(db))
            cursor = await db.execute(
                "UPDATE bank SET bank = bank - ?, crypto = crypto + ? WHERE user = ? AND bank >= ?",
                (cost, amount, user_id, cost),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(await self._read_column(db, user_id, "bank"), cost)
        return cost

    async def sell_crypto(self, user_id: int, amount: int) -> int:
        """Exchange Flyxcoin for bank dollars at the live market price.

        Args:
            user_id: The member's Discord snowflake.
            amount: Coins to sell. Must be positive.

        Returns:
            The dollar amount credited to the bank.

        Raises:
            InsufficientFundsError: If the member holds too few coins.
            ValueError: If ``amount`` is not positive.
        """
        if amount <= 0:
            msg = "sale amount must be positive"
            raise ValueError(msg)

        async with self._transaction() as db:
            await self.ensure_account(user_id)
            proceeds = economy.flx_cost(amount, await self._read_flx_price(db))
            cursor = await db.execute(
                "UPDATE bank SET crypto = crypto - ?, bank = bank + ? "
                "WHERE user = ? AND crypto >= ?",
                (amount, proceeds, user_id, amount),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(
                    await self._read_column(db, user_id, "crypto"), amount, "Flyxcoin"
                )
        return proceeds

    async def set_miner_level(self, user_id: int, level: int) -> None:
        """Set a member's miner level outright. Used by owner-only commands."""
        async with self._transaction() as db:
            await self.ensure_account(user_id)
            await db.execute("UPDATE bank SET miner = ? WHERE user = ?", (level, user_id))

    # ------------------------------------------------------------- lottery --

    async def lottery_state(self) -> LotteryState:
        """Return the pot, the open draw number, and how many have entered."""
        async with self._reader.execute("SELECT pot, draw FROM lottery WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        if row is None:  # pragma: no cover - migration 3 guarantees the row
            return LotteryState(pot=0, draw=1, entrants=0)

        async with self._reader.execute(
            "SELECT COUNT(*) AS n FROM lottery_entries WHERE draw = ?", (row["draw"],)
        ) as cursor:
            count = await cursor.fetchone()
        return LotteryState(
            pot=int(row["pot"]),
            draw=int(row["draw"]),
            entrants=int(count["n"]) if count else 0,
        )

    async def add_to_pot(self, amount: int) -> int:
        """Add the house's take to the pot. A player win never removes from it.

        The caller passes a signed figure (negative when the player won), but
        only a positive contribution is ever applied — a player win is not
        clawed back out of the pot.

        Args:
            amount: Dollars to add. Zero or negative amounts are ignored.

        Returns:
            The pot after the change.
        """
        async with self._transaction() as db:
            if amount > 0:
                await db.execute("UPDATE lottery SET pot = pot + ? WHERE id = 1", (amount,))
            async with db.execute("SELECT pot FROM lottery WHERE id = 1") as cursor:
                row = await cursor.fetchone()
            return int(row["pot"]) if row else 0

    async def enter_lottery(self, user_id: int, price: int) -> bool:
        """Buy this member's single entry into the open draw.

        The charge, the entry, and the pot all move in one transaction, so a
        member cannot be charged without being entered.

        Args:
            user_id: The member entering.
            price: Cost of the entry, charged to the bank balance.

        Returns:
            ``True`` if the entry was recorded, ``False`` if the member had
            already entered this draw.

        Raises:
            InsufficientFundsError: If the bank balance cannot cover the price.
        """
        async with self._transaction() as db:
            await self.ensure_account(user_id)
            async with db.execute("SELECT draw FROM lottery WHERE id = 1") as cursor:
                row = await cursor.fetchone()
            draw = int(row["draw"]) if row else 1

            async with db.execute(
                "SELECT 1 FROM lottery_entries WHERE draw = ? AND user = ?", (draw, user_id)
            ) as cursor:
                if await cursor.fetchone() is not None:
                    return False

            cursor = await db.execute(
                "UPDATE bank SET bank = bank - ? WHERE user = ? AND bank >= ?",
                (price, user_id, price),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(await self._read_column(db, user_id, "bank"), price)

            await db.execute(
                "INSERT INTO lottery_entries (draw, user) VALUES (?, ?)", (draw, user_id)
            )
            # Ticket money is redistributed, never destroyed.
            await db.execute("UPDATE lottery SET pot = pot + ? WHERE id = 1", (price,))
            return True

    async def lottery_entrants(self) -> list[int]:
        """Return everyone entered in the open draw."""
        state = await self.lottery_state()
        async with self._reader.execute(
            "SELECT user FROM lottery_entries WHERE draw = ? ORDER BY user", (state.draw,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [int(row["user"]) for row in rows]

    async def has_entered(self, user_id: int) -> bool:
        """Return whether a member is already in the open draw."""
        state = await self.lottery_state()
        async with self._reader.execute(
            "SELECT 1 FROM lottery_entries WHERE draw = ? AND user = ?",
            (state.draw, user_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def award_lottery(self, winner_id: int) -> int:
        """Pay the pot to a winner and open the next draw.

        Args:
            winner_id: The member who won.

        Returns:
            The dollars paid, credited to the winner's bank balance.
        """
        async with self._transaction() as db:
            async with db.execute("SELECT pot, draw FROM lottery WHERE id = 1") as cursor:
                row = await cursor.fetchone()
            pot = int(row["pot"]) if row else 0
            draw = int(row["draw"]) if row else 1

            await self.ensure_account(winner_id)
            if pot:
                await db.execute("UPDATE bank SET bank = bank + ? WHERE user = ?", (pot, winner_id))
            await db.execute("DELETE FROM lottery_entries WHERE draw = ?", (draw,))
            await db.execute("UPDATE lottery SET pot = 0, draw = draw + 1 WHERE id = 1")
            return pot

    async def roll_over_lottery(self) -> int:
        """Open the next draw without paying out, keeping the pot.

        Returns:
            The pot carried forward.
        """
        async with self._transaction() as db:
            async with db.execute("SELECT pot, draw FROM lottery WHERE id = 1") as cursor:
                row = await cursor.fetchone()
            draw = int(row["draw"]) if row else 1
            await db.execute("DELETE FROM lottery_entries WHERE draw = ?", (draw,))
            await db.execute("UPDATE lottery SET draw = draw + 1 WHERE id = 1")
            return int(row["pot"]) if row else 0

    # ------------------------------------------------------------- jackpot --

    async def jackpot_state(self) -> JackpotState:
        """Return the open round and everyone who has anted into it."""
        async with self._reader.execute("SELECT round FROM jackpot WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        if row is None:  # pragma: no cover - migration 5 guarantees the row
            return JackpotState(round_number=1, entries=())

        round_number = int(row["round"])
        async with self._reader.execute(
            "SELECT user, amount FROM jackpot_entries WHERE round = ? ORDER BY rowid",
            (round_number,),
        ) as cursor:
            rows = await cursor.fetchall()
        return JackpotState(
            round_number=round_number,
            entries=tuple(jackpot.Entry(int(r["user"]), int(r["amount"])) for r in rows),
        )

    async def enter_jackpot(self, user_id: int, amount: int) -> bool:
        """Ante this member into the open round.

        The debit and the entry move in one transaction, so a member cannot be
        charged without being entered, and the pot is always exactly the sum of
        what its entrants paid in.

        One ante per member per round, enforced by the entries table's primary
        key. Topping an ante up would let a member stake past the table limit
        one command at a time, so a second ante is refused rather than added.

        Args:
            user_id: The member entering.
            amount: Dollars to ante, charged to the wallet. Must be positive.

        Returns:
            ``True`` if the ante was recorded, ``False`` if the member was
            already in this round.

        Raises:
            InsufficientFundsError: If the wallet cannot cover the ante.
            ValueError: If ``amount`` is not positive.
        """
        if amount <= 0:
            msg = "an ante must be positive"
            raise ValueError(msg)

        async with self._transaction() as db:
            await self.ensure_account(user_id)
            round_number = await self._read_jackpot_round(db)

            async with db.execute(
                "SELECT 1 FROM jackpot_entries WHERE round = ? AND user = ?",
                (round_number, user_id),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    return False

            cursor = await db.execute(
                "UPDATE bank SET wallet = wallet - ? WHERE user = ? AND wallet >= ?",
                (amount, user_id, amount),
            )
            if cursor.rowcount == 0:
                raise InsufficientFundsError(await self._read_column(db, user_id, "wallet"), amount)

            await db.execute(
                "INSERT INTO jackpot_entries (round, user, amount) VALUES (?, ?, ?)",
                (round_number, user_id, amount),
            )
            return True

    async def award_jackpot(self, winner_id: int, *, cut: int) -> int:
        """Pay the pot to a winner and open the next round.

        The pot is re-read inside the transaction rather than trusted from the
        caller, so the payout is exactly what the entries hold. The caller must
        have closed the round to further antes first; see
        :data:`flyconomy.jackpot.HOUSE_CUT` for how ``cut`` is priced.

        Args:
            winner_id: The member who won.
            cut: Dollars the house keeps, withheld from the payout.

        Returns:
            The dollars paid, credited to the winner's wallet.

        Raises:
            ValueError: If ``cut`` is negative or larger than the pot.
        """
        async with self._transaction() as db:
            round_number = await self._read_jackpot_round(db)
            pot = await self._read_jackpot_pot(db, round_number)
            if not 0 <= cut <= pot:
                msg = f"a cut of {cut} does not fit a pot of {pot}"
                raise ValueError(msg)

            paid = pot - cut
            await self.ensure_account(winner_id)
            if paid:
                await db.execute(
                    "UPDATE bank SET wallet = wallet + ? WHERE user = ?", (paid, winner_id)
                )
            await db.execute("DELETE FROM jackpot_entries WHERE round = ?", (round_number,))
            await db.execute("UPDATE jackpot SET round = round + 1 WHERE id = 1")
            return paid

    async def refund_jackpot(self) -> list[jackpot.Entry]:
        """Hand every ante in the open round back and open the next one.

        Used both for a round that closed without enough entrants to draw and
        on startup, where an open round can only be one a restart interrupted:
        the money is real, but the timer that would have drawn it is gone.

        Returns:
            What was refunded, empty when there was no open round.
        """
        async with self._transaction() as db:
            round_number = await self._read_jackpot_round(db)
            async with db.execute(
                "SELECT user, amount FROM jackpot_entries WHERE round = ? ORDER BY rowid",
                (round_number,),
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                return []

            entries = [jackpot.Entry(int(row["user"]), int(row["amount"])) for row in rows]
            for entry in entries:
                await db.execute(
                    "UPDATE bank SET wallet = wallet + ? WHERE user = ?",
                    (entry.amount, entry.user_id),
                )
            await db.execute("DELETE FROM jackpot_entries WHERE round = ?", (round_number,))
            await db.execute("UPDATE jackpot SET round = round + 1 WHERE id = 1")
            return entries

    @staticmethod
    async def _read_jackpot_round(db: aiosqlite.Connection) -> int:
        """Read the open round number on the given connection."""
        async with db.execute("SELECT round FROM jackpot WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        return int(row["round"]) if row else 1

    @staticmethod
    async def _read_jackpot_pot(db: aiosqlite.Connection, round_number: int) -> int:
        """Read what a round's entries add up to, on the given connection."""
        async with db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS pot FROM jackpot_entries WHERE round = ?",
            (round_number,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["pot"]) if row else 0

    async def delete_account(self, user_id: int) -> bool:
        """Delete a member's account.

        Args:
            user_id: The member's Discord snowflake.

        Returns:
            ``True`` if a row was removed, ``False`` if there was nothing to delete.
        """
        return (await self.purge_user(user_id)).account

    async def purge_user(self, user_id: int) -> PurgeResult:
        """Remove every trace of a member from the database.

        A member lives in three tables: their balances in ``bank``, one row in
        ``lottery_entries`` while a draw is open, and one row in
        ``jackpot_entries`` while a jackpot round is. All three go, in one
        transaction, so a purge can never leave an entry behind that pays a pot
        into an account that no longer exists.

        The lottery pot itself is untouched. Ticket money is redistributed
        rather than held per member, so refunding it here would mint the price
        of a ticket back out of the pot. A jackpot ante is the opposite: that
        pot is only ever the sum of its entries, so dropping the row takes the
        ante out of the pot along with the account that paid it, which is what
        happens to every other balance a purge deletes.

        Args:
            user_id: The member's Discord snowflake. Not validated as a real
                Discord user — purging a bogus id is the point.

        Returns:
            What was deleted.
        """
        async with self._transaction() as db:
            account = await db.execute("DELETE FROM bank WHERE user = ?", (user_id,))
            removed = account.rowcount > 0
            entries = await db.execute("DELETE FROM lottery_entries WHERE user = ?", (user_id,))
            antes = await db.execute("DELETE FROM jackpot_entries WHERE user = ?", (user_id,))
            return PurgeResult(
                account=removed,
                lottery_entries=max(entries.rowcount, 0),
                jackpot_entries=max(antes.rowcount, 0),
            )


# ------------------------------------------------------------- migrations ----


async def _migration_1_base_schema(db: aiosqlite.Connection) -> None:
    """Create the ``bank`` table.

    The DDL matches version 1 of the bot exactly, so this is a no-op against a
    database that the original single-file bot created.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS bank"
        "(wallet INTEGER, bank INTEGER, crypto INTEGER, miner INTEGER, user INTEGER)"
    )


async def _migration_2_unique_user(db: aiosqlite.Connection) -> None:
    """Give ``bank.user`` a unique index, merging any duplicate rows first.

    Version 1 had no uniqueness constraint, and its account-creation path could
    insert a second row for the same member. Duplicates are merged rather than
    dropped: cash and coins are summed and the highest miner level is kept, so
    nobody loses a balance. The index is what makes ``INSERT ... ON CONFLICT``
    and the ranked queries correct.
    """
    async with db.execute("SELECT user FROM bank GROUP BY user HAVING COUNT(*) > 1") as cursor:
        duplicates: Sequence[aiosqlite.Row] = list(await cursor.fetchall())

    for row in duplicates:
        user_id = row["user"]
        log.warning("Merging duplicate rows for user %s", user_id)
        async with db.execute(
            "SELECT COALESCE(SUM(wallet), 0) AS wallet, COALESCE(SUM(bank), 0) AS bank, "
            "COALESCE(SUM(crypto), 0) AS crypto, COALESCE(MAX(miner), 0) AS miner "
            "FROM bank WHERE user = ?",
            (user_id,),
        ) as merged_cursor:
            merged = await merged_cursor.fetchone()
        if merged is None:  # pragma: no cover - an aggregate always returns a row
            continue
        await db.execute("DELETE FROM bank WHERE user = ?", (user_id,))
        await db.execute(
            "INSERT INTO bank (wallet, bank, crypto, miner, user) VALUES (?, ?, ?, ?, ?)",
            (merged["wallet"], merged["bank"], merged["crypto"], merged["miner"], user_id),
        )

    # Rows written before this migration may hold NULL balances if the original
    # bot was interrupted mid-insert. Normalize them so arithmetic is safe.
    await db.execute(
        "UPDATE bank SET wallet = COALESCE(wallet, 0), bank = COALESCE(bank, 0), "
        "crypto = COALESCE(crypto, 0), miner = COALESCE(miner, 0)"
    )
    await db.execute("DELETE FROM bank WHERE user IS NULL")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_user ON bank(user)")


async def _migration_3_lottery(db: aiosqlite.Connection) -> None:
    """Add the lottery pot and its per-draw entries.

    The ``bank`` table is untouched. A season reset can empty these two tables
    without disturbing anything else.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS lottery ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  pot INTEGER NOT NULL DEFAULT 0,"
        "  draw INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    # One row, always. The CHECK above makes a second row impossible.
    await db.execute("INSERT OR IGNORE INTO lottery (id, pot, draw) VALUES (1, 0, 1)")

    # This primary key is what enforces one entry per member per draw, so equal
    # odds cannot be bought by entering twice.
    await db.execute(
        "CREATE TABLE IF NOT EXISTS lottery_entries ("
        "  draw INTEGER NOT NULL,"
        "  user INTEGER NOT NULL,"
        "  PRIMARY KEY (draw, user)"
        ")"
    )


async def _migration_4_market(db: aiosqlite.Connection) -> None:
    """Add the live Flyxcoin price, seeded at :data:`economy.FLX_PRICE`.

    One row, the same shape as ``lottery``. The ``bank`` table is untouched, so
    a season reset can empty this table without disturbing any balance.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS market ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  price INTEGER NOT NULL"
        ")"
    )
    # One row, always. The CHECK above makes a second row impossible.
    await db.execute("INSERT OR IGNORE INTO market (id, price) VALUES (1, ?)", (economy.FLX_PRICE,))


async def _migration_5_jackpot(db: aiosqlite.Connection) -> None:
    """Add the player-funded jackpot round and its antes.

    The ``bank`` table is untouched. There is deliberately no pot column: a
    round's pot is the sum of its entries, so the money in play and the entries
    that own it cannot drift apart. A season reset can empty both tables
    without disturbing any balance.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS jackpot ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  round INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    # One row, always. The CHECK above makes a second row impossible.
    await db.execute("INSERT OR IGNORE INTO jackpot (id, round) VALUES (1, 1)")

    # This primary key is what enforces one ante per member per round, so the
    # table limit cannot be stepped past one top-up at a time.
    await db.execute(
        "CREATE TABLE IF NOT EXISTS jackpot_entries ("
        "  round INTEGER NOT NULL,"
        "  user INTEGER NOT NULL,"
        "  amount INTEGER NOT NULL CHECK (amount > 0),"
        "  PRIMARY KEY (round, user)"
        ")"
    )


_MIGRATIONS: Final = {
    1: _migration_1_base_schema,
    2: _migration_2_unique_user,
    3: _migration_3_lottery,
    4: _migration_4_market,
    5: _migration_5_jackpot,
}
