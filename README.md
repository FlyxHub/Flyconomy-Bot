# Flyconomy Bot

Flyconomy Bot is a Discord economy game. Members earn and bank virtual dollars,
mine a fictional cryptocurrency called Flyxcoin, gamble in a small casino, and
compete on a net-worth leaderboard.

Every command works two ways: as a slash command, such as `/balance`, and as a
classic prefix command, such as `$balance`.

## Contents

- [Features](#features)
- [Before you begin](#before-you-begin)
- [Create a Discord application](#create-a-discord-application)
- [Set up the bot on Windows](#set-up-the-bot-on-windows)
- [Deploy the bot with Docker Compose](#deploy-the-bot-with-docker-compose)
- [Upgrade from version 1](#upgrade-from-version-1)
- [Configuration reference](#configuration-reference)
- [Command reference](#command-reference)
- [Economy reference](#economy-reference)
  - [Slot machine paytable](#slot-machine-paytable)
- [Develop and test](#develop-and-test)
- [Architecture](#architecture)
- [Troubleshoot](#troubleshoot)
- [What changed in version 2](#what-changed-in-version-2)

## Features

- **Banking.** Members hold cash in a wallet and a bank account. Wallet cash can
  be stolen; banked cash cannot.
- **Income.** Members beg for small amounts, collect a daily payout worth 10% of
  their bank balance, or rob another member's wallet.
- **Flyxcoin.** Members buy a miner, upgrade it to improve their odds, mine
  hourly, and buy, sell, or send coins.
- **Casino.** Slot machine, card war, coin flip, rock paper scissors, dice,
  and American roulette.
- **Leaderboards.** Rankings by total net worth and by undeposited wallet cash.

## Before you begin

You need the following:

- **Python 3.13 or later.** Download it from
  [python.org](https://www.python.org/downloads/). The bot is developed and
  tested against Python 3.14.
- **A Discord application** with a bot user. The next section walks you through
  creating one.
- **Docker Engine 24 or later** with the Compose plugin, if you plan to deploy
  to a Linux host.

## Create a Discord application

Do this once, before your first run.

1. Go to the
   [Discord developer portal](https://discord.com/developers/applications) and
   click **New Application**.
2. Open the **Bot** tab, then click **Reset Token** and copy the token. Discord
   shows the token only once. Treat it like a password: anyone who has it
   controls your bot.
3. On the same tab, under **Privileged Gateway Intents**, turn on both of the
   following, then click **Save Changes**:

   - **Message Content Intent**, so the bot can read `$` prefix commands.
   - **Server Members Intent**, so the bot can resolve a member from a name.

4. Open **OAuth2 > URL Generator**. Under **Scopes**, select `bot` and
   `applications.commands`. Under **Bot Permissions**, select **Send Messages**,
   **Embed Links**, and **Read Message History**.
5. Open the generated URL in a browser and invite the bot to your server.

The `applications.commands` scope is what lets the bot publish slash commands.
If you invited the bot before without that scope, invite it again using the new
URL. You don't need to remove the bot first.

## Set up the bot on Windows

Use this path for local development and testing.

1. Clone the repository and change into it:

   ```powershell
   git clone https://github.com/JakeHochstatter/Flyconomy-Bot.git
   cd Flyconomy-Bot
   ```

2. Run the setup script:

   ```powershell
   .\scripts\setup.ps1
   ```

   The script creates a virtual environment in `.venv`, installs the bot and its
   development dependencies, and copies `.env.example` to `.env`.

   If PowerShell refuses to run the script, either unblock it for the current
   session with `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, or
   run it directly with
   `powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1`.

3. Open `.env` and set your token:

   ```ini
   FLYCONOMY_DISCORD_TOKEN=your-token-here
   ```

4. Set `FLYCONOMY_DEV_GUILD_ID` to your test server's ID. Slash commands synced
   to a single server appear immediately, whereas a global sync can take up to
   an hour to reach every client. To copy a server ID, turn on **Developer Mode**
   in Discord under **Settings > Advanced**, then right-click the server and
   select **Copy Server ID**.

5. Start the bot:

   ```powershell
   .\scripts\run.ps1
   ```

   The bot logs `Connected as ...` when it is ready. Press <kbd>Ctrl</kbd>+<kbd>C</kbd>
   to stop it.

To confirm the bot works, type `/balance` in your server. Discord opens the
command picker as you type.

### Windows script reference

| Script | What it does |
| --- | --- |
| `.\scripts\setup.ps1` | Creates `.venv`, installs dependencies, and prepares `.env`. Pass `-Force` to rebuild the virtual environment from scratch, or `-Python <path>` to choose an interpreter. |
| `.\scripts\run.ps1` | Runs the bot. Pass `-LogLevel DEBUG` to trace gateway activity. |
| `.\scripts\check.ps1` | Runs the linter, the type checker, and the tests. Pass `-Fix` to apply automatic fixes, or `-Coverage` for a coverage report. |

## Deploy the bot with Docker Compose

Use this path for production on a Linux host.

1. Clone the repository onto the host and change into it:

   ```bash
   git clone https://github.com/JakeHochstatter/Flyconomy-Bot.git
   cd Flyconomy-Bot
   ```

2. Create the environment file and set your token:

   ```bash
   cp .env.example .env
   $EDITOR .env
   ```

   Leave `FLYCONOMY_DEV_GUILD_ID` empty in production so slash commands sync
   globally. Leave `FLYCONOMY_DATABASE_PATH` alone; Compose overrides it to a
   path on the data volume.

3. Restrict the environment file, because it holds your bot token:

   ```bash
   chmod 600 .env
   ```

4. Build the image and start the bot:

   ```bash
   docker compose up -d --build
   ```

5. Confirm it connected:

   ```bash
   docker compose logs -f
   ```

### Operate the deployment

| Task | Command |
| --- | --- |
| Follow the logs | `docker compose logs -f` |
| Restart the bot | `docker compose restart` |
| Stop the bot | `docker compose down` |
| Upgrade to the latest code | `git pull && docker compose up -d --build` |
| Open a shell in the container | `docker compose exec bot sh` |

The container restarts automatically unless you stop it explicitly, runs as an
unprivileged user with a read-only root filesystem, and drops the ability to
gain new privileges.

### Back up the database

The database lives on the `flyconomy-data` Docker volume, so it survives image
rebuilds. Back it up on a schedule:

```bash
docker compose exec bot sh -c 'cat /data/bot.db' > "backup-$(date +%F).db"
```

To restore a backup, stop the bot, copy the file back into the volume, and start
the bot again:

```bash
docker compose down
docker run --rm -v flyconomy-bot_flyconomy-data:/data -v "$PWD":/backup alpine \
    cp /backup/backup-2026-08-28.db /data/bot.db
docker compose up -d
```

SQLite in write-ahead logging mode keeps recent writes in a `bot.db-wal` file
alongside the database. Stopping the bot first, as shown above, checkpoints that
file so the copy is complete.

## Upgrade from version 1

Version 2 reads the database that version 1 wrote. The `bank` table keeps its
original name, columns, and column order, and no data is discarded.

1. Stop the old bot.
2. Back up `bot.db`. Copy it somewhere outside the repository.
3. Put the database where version 2 expects it:

   - On Windows, `.\scripts\setup.ps1` copies a `bot.db` found in the repository
     root to `data\bot.db` and leaves the original in place as a backup.
   - On Linux with Docker, copy the file onto the volume:

     ```bash
     docker compose up -d --no-start
     docker cp bot.db flyconomy-bot:/data/bot.db
     docker compose up -d
     ```

4. Start the bot. It applies pending migrations at startup and logs each one.

The bot tracks schema state in SQLite's `user_version` pragma, so migrations run
once and are safe to restart. The migration that runs against a version 1
database adds a unique index on `bank.user`. If the old bot ever wrote two rows
for the same member, the migration merges them: it sums the cash and coins and
keeps the higher miner level, then logs a warning naming the member.

Version 2 refuses to start against a database written by a newer build than
itself, rather than risk corrupting it.

## Configuration reference

The bot reads settings from environment variables, or from a `.env` file in the
working directory. Every variable is prefixed with `FLYCONOMY_`.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `FLYCONOMY_DISCORD_TOKEN` | Yes | None | Bot token from the Discord developer portal. |
| `FLYCONOMY_DATABASE_PATH` | No | `data/bot.db` | Path to the SQLite file. Parent directories are created. |
| `FLYCONOMY_COMMAND_PREFIX` | No | `$` | Prefix for classic text commands. Slash commands ignore it. |
| `FLYCONOMY_TIMEZONE` | No | `America/Chicago` | IANA timezone for embed timestamps. |
| `FLYCONOMY_LOG_LEVEL` | No | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `FLYCONOMY_DEV_GUILD_ID` | No | None | Server to sync slash commands to. Set it while developing; leave it empty in production. |
| `FLYCONOMY_ALWAYS_MINE_USER_IDS` | No | Empty | Comma-separated user IDs whose mine attempts always succeed. |

The bot validates settings before it connects and exits with code `2` when a
value is missing or malformed, naming the field that failed.

## Command reference

Run any command as `/name` or as `$name`. The prefix is configurable, and
mentioning the bot works as a prefix too.

### Banking and income

| Command | Description |
| --- | --- |
| `balance [member]` | Shows wallet, bank, Flyxcoin, miner level, and net worth. Defaults to you. Alias: `bal`. |
| `deposit [amount]` | Moves money from your wallet to your bank. Defaults to your whole wallet. Alias: `dep`. |
| `withdraw [amount]` | Moves money from your bank to your wallet. Defaults to your whole bank balance. |
| `beg` | Pays $1 to $100 half the time. Cooldown: 3 seconds. |
| `daily` | Pays 10% of your bank balance. Cooldown: 24 hours. |
| `rob <member>` | Takes a random share of a member's wallet, half the time. Cooldown: 1 hour. |
| `leaderboard` | Ranks the top 10 members by net worth. Alias: `lb`. |
| `wallets` | Ranks the top 10 undeposited wallets, which are the best robbery targets. |

### Flyxcoin

| Command | Description |
| --- | --- |
| `mine` | Mines Flyxcoin with your miner. Requires a miner. Cooldown: 1 hour. |
| `upgrade` | Raises your miner one level, paid from your bank balance. |
| `flx` | Shows how much Flyxcoin is in circulation and what it is worth. |
| `flx buy [amount]` | Buys Flyxcoin with bank money. Defaults to as many as you can afford. |
| `flx sell [amount]` | Sells Flyxcoin into your bank. Defaults to everything you hold. |
| `flx send <member> <amount>` | Sends Flyxcoin to another member. |

As a slash command, the bare `flx` form is `/flx info`.

### Casino

Every game stakes money from your wallet.

| Command | Description |
| --- | --- |
| `coinflip <heads\|tails> <bet>` | Returns 2x your stake on a correct call. Alias: `cf`. |
| `rps <rock\|paper\|scissors> <bet>` | Returns 3x your stake on a win, and refunds your stake on a tie. |
| `dice <1-6> <bet>` | Returns 6x your stake on a correct call. |
| `roulette <red\|black\|0-36\|00> <bet>` | Returns 2x on a color and 35x on a single pocket. |
| `slots <bet>` | Spins three reels. Three of a kind returns 9x to 55x. Alias: `slot`. |
| `war <bet>` | Draws a card against the dealer. The higher card returns 2x, and a tie is returned. |

### Owner commands

These are prefix-only. A slash command appears in every member's command picker,
which is the wrong place to advertise a command nobody else can run.

| Command | Description |
| --- | --- |
| `$adminme` | Gives you the admin miner, level 999. |
| `$adminmine <amount>` | Adds Flyxcoin to your own account. A negative amount removes coins. |
| `$reset <member>` | Deletes a member's account, resetting them to a new player. |
| `$sync` | Republishes slash commands to Discord. Run this after adding or renaming a command. |

## Economy reference

### Currency

| Rule | Value |
| --- | --- |
| Starting wallet | $0 |
| Starting bank | $1,000 |
| Flyxcoin price | $10,000, for both buying and selling |
| Net worth | wallet + bank + (Flyxcoin x $10,000) |

### Miner levels

`upgrade` pays from your bank balance and raises your miner one level. `mine`
runs once per hour.

| Level | Upgrade cost | Chance to mine a coin |
| --- | --- | --- |
| 0 | $100 | No miner |
| 1 | $5,000 | 1% |
| 2 | $20,000 | 5% |
| 3 | $100,000 | 10% |
| 4 | $500,000 | 15% |
| 5 | Maximum | 20% |
| 999 | Owner only | Always mines 10 coins |

### Casino payouts

Each game debits your stake when you place the bet, then credits the return
below if you win. The profit column is what you gain overall.

| Game | Win chance | Returned | Net profit | House edge |
| --- | --- | --- | --- | --- |
| Coin flip | 1 in 2 | 2x stake | 1x stake | 0% |
| Dice | 1 in 6 | 6x stake | 5x stake | 0% |
| War | 47.06%, plus a 5.88% tie | 2x stake | 1x stake | 0% |
| Slots | 1 in 6 | 2x to 55x stake | 1x to 54x stake | 4.17% |
| Roulette, color | 18 in 38 | 2x stake | 1x stake | 5.26% |
| Roulette, single pocket | 1 in 38 | 35x stake | 34x stake | 7.89% |
| Rock paper scissors | 1 in 3, plus a 1 in 3 refunded tie | 3x stake | 2x stake | **-33.33%** |

House edge is the share of each staked dollar the bot keeps on average. A
negative figure means the game pays players more than its odds justify.

Rock paper scissors is the outlier: it returns 3x on a one-in-three win, so
players gain a third of everything they stake on it. That carries over from
version 1 unchanged rather than being rebalanced without asking. To change it,
edit `RPS_RETURN` in `src/flyconomy/economy.py`; nothing else needs to change.

### Slot machine paytable

Three identical reels, each carrying six equally likely symbols, for 216
possible spins. One spin in six pays.

| Result | Returns |
| --- | --- |
| 💎 💎 💎 | 55x stake |
| ⭐ ⭐ ⭐ | 35x stake |
| 🔔 🔔 🔔 | 22x stake |
| 🍇 🍇 🍇 | 15x stake |
| 🍋 🍋 🍋 | 11x stake |
| 🍒 🍒 🍒 | 9x stake |
| Exactly two 💎 or two ⭐ | 2x stake |
| Anything else | Nothing |

Slots is the swingiest game in the casino. A single spin varies by about 4.85
times the stake, so a few hundred spins can land far from the 4.17% average in
either direction. The other games are much steadier.

If you retune a payout, `tests/test_economy.py` enumerates all 216 spins and
asserts the house edge, so it fails until you update the expected value here and
in the test.

## Develop and test

### Run the checks

```powershell
.\scripts\check.ps1
```

On Linux or macOS, run the same tools directly:

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest
```

### Run a single test

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_economy.py::TestRoulette -q
.\.venv\Scripts\python.exe -m pytest -k roulette -q
```

The suite is organized by concern:

| File | Covers |
| --- | --- |
| `test_economy.py` | The pure rules: payouts, mine odds, the roulette wheel. |
| `test_database.py` | Balances, transfers, and concurrency guarantees. |
| `test_migrations.py` | Upgrading a real version 1 database without losing data. |
| `test_commands.py` | That every version 1 command and alias still registers. |
| `test_cog_behavior.py` | The command bodies, against a real database. |
| `test_admin_and_startup.py` | Owner commands, logging, and process exit codes. |
| `test_config.py`, `test_bot.py` | Settings validation and error message translation. |

### Add a command

1. Add the method to the cog it belongs to, in `src/flyconomy/cogs/`. Use
   `@commands.hybrid_command` so the command works as both a slash command and a
   prefix command.
2. Put any new tunable number in `src/flyconomy/economy.py` rather than inline.
3. Add a test to `tests/`.
4. Restart the bot and run `$sync` so Discord learns about the new command.

### Change the database schema

1. Add a migration function at the bottom of `src/flyconomy/database.py`.
2. Register it in the `_MIGRATIONS` mapping under the next version number.
3. Raise `SCHEMA_VERSION` to match.

Migrations run in order at startup and each one runs once. Write them to be safe
against a database that already satisfies them, because a partly upgraded
database is retried from the last version that finished.

## Architecture

```
src/flyconomy/
├── __main__.py       Entry point: loads settings, starts the bot, maps failures to exit codes
├── bot.py            The client: extension loading, slash command sync, error handling
├── config.py         Settings, validated from the environment
├── database.py       SQLite access and schema migrations
├── economy.py        Every tunable number and the pure rules that use them
├── embeds.py         Message and embed builders
├── errors.py         Exceptions the bot raises deliberately
├── logging_config.py Logging setup
└── cogs/             One module per command group
```

Three ideas hold the layout together:

- **Rules are pure.** `economy.py` imports nothing from `discord`, so game
  balance is unit tested without a gateway connection, and rebalancing never
  means touching command code.
- **Money moves in the database, not in Python.** Every balance change is a
  relative SQL update guarded in its own `WHERE` clause, such as
  `SET wallet = wallet + ? WHERE user = ? AND wallet + ? >= 0`. Two commands
  running at once cannot lose an update or push a balance negative. Compound
  moves, such as a transfer between two members, run in one transaction.
- **Errors are handled centrally.** `bot.py` translates an exception into a
  member-facing message in one place, for both slash and prefix commands, so a
  new command inherits cooldown, permission, and insufficient-funds messages
  without writing its own error handler.

## Troubleshoot

### Slash commands don't appear in Discord

Set `FLYCONOMY_DEV_GUILD_ID` to your server ID and restart. A guild sync is
immediate; a global sync can take up to an hour. If commands still don't appear,
re-invite the bot with the `applications.commands` scope, then run `$sync`.

### The bot exits with "Discord refused the privileged intents"

Turn on the **Message Content** and **Server Members** intents on the **Bot** tab
of the Discord developer portal, then restart the bot.

### The bot exits with "Configuration is invalid"

The log names the field that failed. The usual cause is an empty
`FLYCONOMY_DISCORD_TOKEN` in `.env`.

### Prefix commands are ignored but slash commands work

The **Message Content** intent is off, so the bot receives an empty string for
every message. Turn it on in the developer portal.

### "database is locked"

Two processes are writing to the same SQLite file. Make sure only one instance
of the bot is running against a given database.

### PowerShell won't run the scripts

Windows blocks unsigned scripts by default. Allow them for the current session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## What changed in version 2

The bot keeps every version 1 feature and its database. The following behavior
changed on purpose.

| Change | Reason |
| --- | --- |
| Commands work as slash commands as well as `$` prefix commands. | Discord's preferred interface, and it gives members argument hints. |
| Two games were added: `slots` and `war`. | The casino had no jackpot game and no game with a push. Both are documented in the payout table above. |
| Mining odds at levels 2 through 5 are now 5%, 10%, 15%, and 20%. | Version 1 tested `randint(1, 100) in range(1, 5)`, which is 4%, not the 5% it announced. Every level was short by one point. The odds now match what the bot has always claimed. |
| Roulette has a real `00` pocket. | Python reads the literal `00` as `0`, so version 1's wheel held two `0` pockets and no `00`. Betting on `0` paid at double the correct rate. |
| Negative bets are rejected. | Version 1 compared `bet > wallet` and then subtracted the bet, so a negative bet added money to the wallet. |
| Robbing yourself is refused. | Version 1 announced the refusal and then robbed you anyway. |
| A losing balance can never go negative. | Balance checks are now part of the SQL update instead of a separate read. |
| Owner commands are prefix-only. | A slash command is published to every member, including the ones who can't run it. |
| The token is read from the environment. | Version 1 kept the token in the source file. |

Version 1 is preserved in git history at commit `8e9226e`.
