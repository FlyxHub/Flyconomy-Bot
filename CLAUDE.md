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
`database.py`, with `economy.py` and `blackjack.py` holding the rules, `views.py` the interactive
buttons, `ratelimit.py` the abuse throttle, and `config.py` the settings.

Three invariants hold the design together. Breaking one is how this codebase regresses:

1. **The rules modules import nothing from `discord`.** Every tunable number and every pure rule
   lives in `economy.py`, or in `blackjack.py` for that one ruleset, and is unit tested without a
   gateway. New game logic goes there, not in a cog. A ruleset big enough to carry its own types
   earns its own module beside them; anything smaller belongs in `economy.py`.

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
- **Interactive components** live in `views.py`. Keep the button callbacks trivial: each one calls an
  `apply_*` coroutine that takes no `Interaction`, then redraws. That split is what lets
  `tests/test_views.py` drive a whole hand against a real database with no gateway. A view that
  moves money must be idempotent — a click and a timeout can both reach it, so `BlackjackView.settle`
  guards with a `_settled` flag.
- Each cog decorator carries `# type: ignore[arg-type]`: discord.py's hybrid decorators are typed for
  pyright and mypy infers `Never` parameters. The ignores are narrow on purpose — mypy runs strict.

## Anti-abuse

**The load-bearing invariant: no game may have a positive expected value.** A game that profits per
play is a money printer, and a rate limit only changes how fast it prints. `tests/test_antiabuse.py`
asserts this for every game and fails if a payout is retuned into positive territory — treat that
test as a spec, not as something to adjust until it passes.

Version 1's RPS refunded ties, which paid +33%; ties now go to the house, leaving it at exactly 0%.
That was the actual exploit behind "people spam games for guaranteed profit", not the missing rate
limits.

Three further layers, all in place because they cover different failure modes:

- **Faucet cooldowns.** `beg` creates money from nothing; its cooldown is the only thing bounding it.
  It sits at 60s so it earns less per hour than a maximum-level miner. Check that ratio before
  touching either number.
- **A shared rate limit,** in `BaseCog.cog_check` over `ratelimit.SlidingWindowLimiter`. Deliberately
  *not* per-command: a per-command cooldown is dodged by rotating between games, and cannot cover
  commands that refund their own cooldown when they decline to act (`mine` without a miner, `rob` on
  an empty wallet), which would otherwise loop for free.
- **A table limit,** `settings.max_bet`, enforced in `Gambling._stake`. Every wager debits through
  that one method, so a new game cannot forget the cap. Check the limit *before* debiting, so a
  refused bet costs nothing.

`RateLimitedError` subclasses `commands.CheckFailure` on purpose. discord.py's `Bot.invoke` only
dispatches `CommandError` subclasses to an error handler, so a plain exception raised from a cog
check reaches the member as silence plus a logged traceback.

## Preserved quirks

- **`always_mine_user_ids`** exists because version 1 hardcoded one Discord user ID for guaranteed
  mining. It's now a config setting rather than a literal in the source.
- **Coinflip and dice look generous but are exactly fair.** Dice returns 6x on 1-in-6 odds, which is
  an edge of zero. Don't "fix" them.

Games added since the rewrite carry a **deliberate, documented house edge**. Where the outcome
space is small enough, the tests enumerate it rather than sampling: `test_economy.py` walks all 216
slot spins and all 2,652 war hands and asserts the exact return. Blackjack's edge depends on player
strategy, so `test_blackjack.py` bounds it by simulation across several strategies instead of
asserting one figure. Retuning a payout fails those tests until the expected value is updated in
both the test and the README.

What was *not* preserved is listed in the README's "What changed in version 2" table — off-by-one
mine odds, the missing `00` pocket, negative bets, and robbing yourself were all fixed.
