"""Shared test fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from flyconomy.database import Database

#: A user ID that no real Discord account will collide with in tests.
ALICE = 111_111_111_111_111_111
BOB = 222_222_222_222_222_222
CAROL = 333_333_333_333_333_333


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a path to a database file inside a per-test directory."""
    return tmp_path / "bot.db"


@pytest.fixture
async def db(db_path: Path) -> AsyncIterator[Database]:
    """Open a migrated, empty database for one test."""
    database = await Database.connect(db_path)
    try:
        yield database
    finally:
        await database.close()


def make_v1_database(path: Path, rows: list[tuple[int, int, int, int, int]]) -> None:
    """Create a database exactly as version 1 of the bot would have left it.

    Args:
        path: Where to write the file.
        rows: ``(wallet, bank, crypto, miner, user)`` tuples to insert, matching
            the column order version 1 used.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS bank"
            "(wallet INTEGER, bank INTEGER, crypto INTEGER, miner INTEGER, user INTEGER)"
        )
        connection.executemany("INSERT INTO bank VALUES (?, ?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()
