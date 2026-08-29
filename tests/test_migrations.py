"""Tests that a version 1 database survives the upgrade intact."""

from __future__ import annotations

import sqlite3

import pytest

from flyconomy import database as database_module
from flyconomy import economy
from flyconomy.database import Database
from tests.conftest import ALICE, BOB, make_v1_database


async def test_a_version_1_database_keeps_every_balance(db_path):
    make_v1_database(
        db_path,
        [
            (250, 9_000, 3, 2, ALICE),
            (0, 1_000, 0, 0, BOB),
        ],
    )

    database = await Database.connect(db_path)
    try:
        alice = await database.get_account(ALICE)
        assert (alice.wallet, alice.bank, alice.crypto, alice.miner) == (250, 9_000, 3, 2)

        bob = await database.get_account(BOB)
        assert (bob.wallet, bob.bank, bob.crypto, bob.miner) == (0, 1_000, 0, 0)
    finally:
        await database.close()


async def test_the_bank_table_keeps_its_original_columns(db_path):
    make_v1_database(db_path, [(1, 2, 3, 4, ALICE)])
    database = await Database.connect(db_path)
    await database.close()

    connection = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(bank)")]
    finally:
        connection.close()

    assert columns == ["wallet", "bank", "crypto", "miner", "user"]


async def test_migrating_records_the_schema_version(db_path):
    make_v1_database(db_path, [])
    database = await Database.connect(db_path)
    await database.close()

    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()

    assert version == database_module.SCHEMA_VERSION


async def test_migrating_is_idempotent(db_path):
    make_v1_database(db_path, [(250, 9_000, 3, 2, ALICE)])

    for _ in range(3):
        database = await Database.connect(db_path)
        try:
            await database.migrate()
            account = await database.get_account(ALICE)
        finally:
            await database.close()
        assert account.bank == 9_000


async def test_duplicate_rows_are_merged_rather_than_dropped(db_path):
    # Version 1 could insert a second row for one member, because nothing
    # enforced uniqueness on the user column.
    make_v1_database(
        db_path,
        [
            (100, 1_000, 1, 3, ALICE),
            (50, 500, 2, 1, ALICE),
        ],
    )

    database = await Database.connect(db_path)
    try:
        account = await database.get_account(ALICE)
    finally:
        await database.close()

    assert account.wallet == 150
    assert account.bank == 1_500
    assert account.crypto == 3
    assert account.miner == 3


async def test_the_unique_index_prevents_new_duplicates(db_path):
    make_v1_database(db_path, [(0, 1_000, 0, 0, ALICE)])
    database = await Database.connect(db_path)
    try:
        await database.ensure_account(ALICE)
        await database.ensure_account(ALICE)
    finally:
        await database.close()

    connection = sqlite3.connect(db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM bank WHERE user = ?", (ALICE,)).fetchone()[
            0
        ]
    finally:
        connection.close()

    assert count == 1


async def test_null_balances_are_normalized_to_zero(db_path):
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE bank(wallet INTEGER, bank INTEGER, crypto INTEGER, "
            "miner INTEGER, user INTEGER)"
        )
        connection.execute("INSERT INTO bank VALUES (NULL, NULL, NULL, NULL, ?)", (ALICE,))
        connection.commit()
    finally:
        connection.close()

    database = await Database.connect(db_path)
    try:
        account = await database.get_account(ALICE)
    finally:
        await database.close()

    assert (account.wallet, account.bank, account.crypto, account.miner) == (0, 0, 0, 0)


async def test_a_newer_schema_is_refused(db_path):
    make_v1_database(db_path, [])
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"PRAGMA user_version = {database_module.SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="newer than this build"):
        await Database.connect(db_path)


async def test_a_fresh_database_is_created_at_the_current_version(db_path):
    database = await Database.connect(db_path)
    await database.close()

    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        indexes = [row[1] for row in connection.execute("PRAGMA index_list(bank)")]
    finally:
        connection.close()

    assert version == database_module.SCHEMA_VERSION
    assert "idx_bank_user" in indexes


async def test_a_version_1_database_gets_the_market_seeded_at_the_base_price(db_path):
    make_v1_database(db_path, [(0, 1_000, 0, 0, ALICE)])

    database = await Database.connect(db_path)
    try:
        price = await database.get_flx_price()
    finally:
        await database.close()

    assert price == economy.FLX_PRICE


async def test_the_market_migration_is_idempotent(db_path):
    make_v1_database(db_path, [])

    for _ in range(3):
        database = await Database.connect(db_path)
        try:
            await database.migrate()
            price = await database.get_flx_price()
        finally:
            await database.close()
        assert price == economy.FLX_PRICE
