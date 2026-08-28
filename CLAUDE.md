# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A Discord economy bot built on discord.py 2.7 with `aiosqlite` persistence. Members bank virtual
dollars, mine "Flyxcoin", and gamble. Every member-facing command is a **hybrid command**: it works
as `/balance` and as `$balance` from one definition.

Version 2 is a rewrite of a single 679-line `econBot.py` (git history, commit `8e9226e`). The rewrite
kept every feature and the original database file. `README.md` has the full command and economy
reference; don't duplicate it here.

## Commands

```powershell
.\scripts\setup.ps1              # venv + editable install + .env  (-Force to rebuild)
.\scripts\run.ps1                # run the bot        (-LogLevel DEBUG)
.\scripts\check.ps1              # ruff + mypy + pytest  (-Fix, -Coverage)
```

Run a single test with the venv interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_economy.py::TestRoulette -q
.\.venv\Scripts\python.exe -m pytest -k migration -q
```

`check.ps1` runs every check even after one fails, so a single pass reports everything.

## Architecture

`src/flyconomy/` — `__main__` (entry point) → `bot.py` (client) → `cogs/` (commands) over
`database.py`, with `economy.py` holding the rules and `config.py` the settings.

Three invariants hold the design together. Breaking one is how this codebase regresses:

1. **`economy.py` imports nothing from `discord`.** Every tunable number and every pure rule
   (mine odds, roulette payouts, RPS outcomes) lives there and is unit tested without a gateway.
   New game logic goes here, not in a cog.

2. **Money moves in SQL, never read-modify-write in Python.** Every balance change is a relative
   update with the guard in its own `WHERE` clause:
   `SET wallet = wallet + ? WHERE user = ? AND wallet + ? >= 0`. A `rowcount` of 0 means the guard
   refused it, which becomes `InsufficientFundsError`. Compound moves (`steal`, `transfer_crypto`,
   `buy_crypto`) run inside `_transaction()`. Version 1 lost updates and allowed negative balances
   precisely because it read into Python first — don't reintroduce that pattern.

3. **Errors are translated in one place.** `bot.describe_command_error` maps an exception to a
   member-facing string for both slash and prefix paths. A new command inherits cooldown,
   permission, and insufficient-funds messages for free. Do not add per-command `@cmd.error`
   handlers; version 1 needed one per command and they drifted.

## Database

One table, inherited verbatim from version 1 and **not to be renamed or reordered**:

```sql
bank(wallet INTEGER, bank INTEGER, crypto INTEGER, miner INTEGER, user INTEGER)
```

Schema state lives in SQLite's `user_version` pragma. To change the schema: add a migration function
at the bottom of `database.py`, register it in `_MIGRATIONS` under the next integer, and bump
`SCHEMA_VERSION`. Migrations are forward-only and must be safe to re-run — a partly upgraded database
retries from the last version that finished. Migration 2 is the one that runs against a real v1
database; it merges duplicate `user` rows before adding the unique index.

`tests/test_migrations.py` builds a genuine v1 database with `make_v1_database()` and asserts nothing
is lost. Keep that passing.

## Conventions

- **Adding a command:** put it in the cog it belongs to, decorate with `@commands.hybrid_command`,
  and give it a docstring — the docstring becomes both the `$help` text and the slash description.
  Annotate numeric arguments with `commands.Range[int, 1]` so Discord rejects bad input client-side
  and negative bets stay impossible.
- **Owner commands stay prefix-only** (`@commands.command` in `cogs/admin.py`). A slash command is
  published to every member, including those who can't run it. `$sync` republishes the tree.
- **`self.rng`** on `BaseCog` is the random source for game outcomes, so tests can seed it.
- Each cog decorator carries `# type: ignore[arg-type]`: discord.py's hybrid decorators are typed for
  pyright and mypy infers `Never` parameters. The ignores are narrow on purpose — mypy runs strict.

## Preserved quirks

These look like bugs but are deliberate, carried over so the economy doesn't shift under players:

- **RPS and dice overpay.** RPS returns 3x the stake on a 1-in-3 win, dice returns 6x on a 1-in-6 win,
  and the RPS win message says `bet * 2` while the credit is `bet * 3`. Both games are player-positive
  over time. Version 1 did exactly this. Change it only if asked to rebalance.
- **`always_mine_user_ids`** exists because version 1 hardcoded one Discord user ID for guaranteed
  mining. It's now a config setting rather than a literal in the source.

What was *not* preserved is listed in the README's "What changed in version 2" table — off-by-one
mine odds, the missing `00` pocket, negative bets, and robbing yourself were all fixed.
