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
  - [Keeping the economy honest](#keeping-the-economy-honest)
  - [Surviving a season](#surviving-a-season)
  - [The lottery](#the-lottery)
  - [Blackjack](#blackjack)
  - [Crash](#crash)
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
  hourly, and buy, sell, or send coins. The price moves on its own every 5
  minutes on a bounded random walk, and the bot's status shows it live as a
  `FLX: $10,340 ▲2.1%` stock ticker.
- **Casino.** Blackjack with hit, stand, and double-down buttons, plus a slot
  machine, card war, coin flip, rock paper scissors, dice, and American
  roulette.
- **Lottery.** A pot fed by entry fees and a share of the casino's winnings,
  drawn on a schedule. One entry each, so everyone has the same chance.
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
   git clone https://github.com/FlyxHub/Flyconomy-Bot.git
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
   git clone https://github.com/FlyxHub/Flyconomy-Bot.git
   cd Flyconomy-Bot
   ```

2. Run the deploy script:

   ```bash
   ./scripts/deploy.sh
   ```

   The script installs the Docker Compose plugin if it is missing, creates
   `.env` from `.env.example` on first run, restricts its permissions, and
   runs `docker compose up -d --build`. Re-run it any time, including after
   `git pull`, to rebuild and restart.

3. Set your token in `.env`, then re-run the script:

   ```bash
   $EDITOR .env
   ./scripts/deploy.sh
   ```

   Leave `FLYCONOMY_DEV_GUILD_ID` empty in production so slash commands sync
   globally. Leave `FLYCONOMY_DATABASE_PATH` alone; Compose overrides it to a
   path on the data volume.

4. Confirm it connected:

   ```bash
   docker compose logs -f
   ```

Prefer to run the steps yourself instead of the script? Create `.env` from
`.env.example`, `chmod 600 .env` because it holds your bot token, then run
`docker compose up -d --build`.

### Operate the deployment

| Task | Command |
| --- | --- |
| Follow the logs | `docker compose logs -f` |
| Restart the bot | `docker compose restart` |
| Stop the bot | `docker compose down` |
| Upgrade to the latest code | `git pull && ./scripts/deploy.sh` (or `git pull && docker compose up -d --build`) |
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
| `FLYCONOMY_MAX_DAILY_PAYOUT` | No | `10000` | Ceiling on one `daily` claim. This is what bounds a season. |
| `FLYCONOMY_LOTTERY_TICKET_PRICE` | No | `10000` | Cost of one lottery entry. |
| `FLYCONOMY_LOTTERY_RAKE` | No | `0.25` | Share of the casino's net winnings added to the pot. |
| `FLYCONOMY_LOTTERY_DRAW_HOURS` | No | `24` | Hours between lottery draws. |
| `FLYCONOMY_CREATOR_TAX_RATE` | No | `0.05` | Share of the casino's net winnings paid to `FLYCONOMY_CREATOR_TAX_USER_ID`, carved out of the share the lottery rake leaves for destruction. |
| `FLYCONOMY_CREATOR_TAX_USER_ID` | No | None | Bank account credited with the creator tax. Unset disables the tax outright, regardless of the rate. |
| `FLYCONOMY_MAX_BET` | No | `100000` | Table limit: the most a member may stake on one wager. |
| `FLYCONOMY_RATE_LIMIT_ACTIONS` | No | `6` | Game commands a member may run per window. |
| `FLYCONOMY_RATE_LIMIT_SECONDS` | No | `10` | Length of that window, in seconds. |
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
| `beg` | Pays $1 to $100 half the time. Cooldown: 60 seconds. |
| `daily` | Pays 10% of your bank balance. Cooldown: 24 hours. |
| `rob <member>` | Takes a random share of a member's wallet, half the time. Cooldown: 1 hour. |
| `leaderboard` | Ranks the top 10 members by net worth. Alias: `lb`. |
| `wallets` | Ranks the top 10 undeposited wallets, which are the best robbery targets. |
| `resetme` | Deletes your own account, resetting you to a new player. |

### Flyxcoin

| Command | Description |
| --- | --- |
| `mine` | Mines Flyxcoin with your miner. Requires a miner. Cooldown: 1 hour. |
| `upgrade` | Raises your miner one level, paid from your bank balance. |
| `flx` | Shows the current Flyxcoin price, how much is in circulation, and what it is worth. |
| `flx buy [amount]` | Buys Flyxcoin with bank money. Defaults to as many as you can afford. |
| `flx sell [amount]` | Sells Flyxcoin into your bank. Defaults to everything you hold. |
| `flx send <member> <amount>` | Sends Flyxcoin to another member. |

As a slash command, the bare `flx` form is `/flx info`.

### Casino

Every game stakes money from your wallet.

| Command | Description |
| --- | --- |
| `coinflip <heads\|tails> <bet>` | Returns 2x your stake on a correct call. Alias: `cf`. |
| `rps <rock\|paper\|scissors> <bet>` | Returns 3x your stake on a win. The house takes ties. |
| `dice <1-6> <bet>` | Returns 6x your stake on a correct call. |
| `roulette <red\|black\|0-36\|00> <bet>` | Returns 2x on a color and 35x on a single pocket. |
| `blackjack <bet>` | Deals a hand against the dealer, with buttons to hit, stand, or double down. Alias: `bj`. |
| `crash <bet>` | A multiplier climbs from 1.00x. Press Cash Out before it crashes to lock in the payout. |
| `slots <bet>` | Spins three reels. Three of a kind returns 9x to 55x. Alias: `slot`. |
| `war <bet>` | Draws a card against the dealer. The higher card returns 2x, and a tie is returned. |

### Lottery

| Command | Description |
| --- | --- |
| `lottery` | Shows the pot, the entrants, and whether you are in. |
| `lottery enter` | Enters the current draw. One entry per member, paid from your bank. |
| `lottery entrants` | Lists who is in the current draw. |

As a slash command, the bare `lottery` form is `/lottery info`.

### Owner commands

These are prefix-only. A slash command appears in every member's command picker,
which is the wrong place to advertise a command nobody else can run.

| Command | Description |
| --- | --- |
| `$adminme` | Gives you the admin miner, level 999. |
| `$adminmine <amount>` | Adds Flyxcoin to your own account. A negative amount removes coins. |
| `$reset <member>` | Deletes another member's account, resetting them to a new player. Members reset themselves with `resetme`. |
| `$sync` | Republishes slash commands to Discord. Run this after adding or renaming a command. |
| `$draw` | Runs a lottery draw immediately instead of waiting for the schedule. |

## Economy reference

### Currency

| Rule | Value |
| --- | --- |
| Starting wallet | $0 |
| Starting bank | $1,000 |
| Flyxcoin price | Starts at $10,000, both buying and selling always trade at the current live price |
| Flyxcoin price range | $5,000 to $20,000 (50% to 200% of the starting price) |
| Flyxcoin price tick | Every 5 minutes: a random move of up to 3%, pulled 5% of the way back toward $10,000 first |
| Net worth | wallet + bank + (Flyxcoin x the live Flyxcoin price) |

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
| Rock paper scissors | 1 in 3 | 3x stake | 2x stake | 0% |
| Blackjack | Depends on how you play | 2x stake, or 2.5x for a natural | 1x to 1.5x stake | 1.4% to 15.8% |
| Crash | Depends on when you cash out | Whatever multiplier you cash out at | Multiplier minus 1, times stake | 3%, flat at every cash-out target |

House edge is the share of each staked dollar the bot keeps on average. **No
game pays players more than its odds justify**, which is enforced by a test: if
you retune a payout into positive territory, the suite fails.

Rock paper scissors keeps its 3x win but no longer refunds a tie. Refunding the
tie is what made it pay +33%, and a game that profits per play cannot be fixed
by any rate limit.

### Keeping the economy honest

Three separate things stop members from farming the bot. They cover different
failure modes, so none of them substitutes for the others.

**No game has a positive expected value.** This is the one that matters. A game
that profits per play is a money printer, and slowing it down with a cooldown
only changes how long the printing takes. Every payout in the table above is
either fair or favours the house, and `tests/test_antiabuse.py` fails if that
stops being true.

**Faucets are slower than mining.** `beg` is the only command that creates money
with no stake and no real limit, so its cooldown is what bounds it. At 60 seconds
it produces about $1,500 an hour, just under a maximum-level miner.

**A shared rate limit, not a per-command cooldown.** Every game command spends
from one budget of `FLYCONOMY_RATE_LIMIT_ACTIONS` per
`FLYCONOMY_RATE_LIMIT_SECONDS`, per member. A per-command cooldown would be
sidestepped by rotating between games, and it cannot cover a command that
refunds its own cooldown when it declines to act, such as mining without a miner.

**A table limit.** `FLYCONOMY_MAX_BET` caps a single wager. It stops a doubling
strategy from escalating without bound, and it bounds how much damage a
mispriced game could do before anyone notices. Bets above it are refused with a
message naming the limit; nothing is silently clamped, and a refused bet costs
nothing. Doubling down in blackjack can take a hand to twice the limit, which is
deliberate and matches how a real table works.

A doubling strategy will still end most short sessions slightly ahead. That is
true of any fair game and cannot be designed away without making the games
unfair. What matters is the average, which is now zero or negative everywhere.

### Surviving a season

The economy is meant to run from one January 1 to the next and then be reset, so
the question is not whether it inflates but whether it stays readable for 365
days. It does, because **only one source ever compounded**.

`daily` pays a tenth of your bank. A percentage of a growing number is
exponential: at 10% a day that is a factor of 1.28e15 over a year, which is more
money than everything else in the bot produces by fifteen orders of magnitude.
Every other source is linear, and linear cannot run away inside a fixed season.

So `daily` is capped by `FLYCONOMY_MAX_DAILY_PAYOUT`. Below ten times the cap
nothing changes, which is most of the early game; above it, growth becomes a
straight line.

| | Total supply after 365 days | Richest member |
| --- | --- | --- |
| Uncapped | 72,576,191,108,407,800,168 | 50,200,674,449,656,928,049 |
| Capped at $10,000 | 90,953,611 | 22,221,190 |

Measured over a simulated year with forty members, a third of them grinding.
`tests/test_season.py` runs a season on every commit and fails if the supply or
the richest member leaves sane bounds, or if growth stops looking linear.

To make a season shorter or longer, move the cap: it is very close to the only
number that decides how big the endgame gets.

### The lottery

A pot that redistributes money rather than creating it. It is fed from two
places, and both matter:

- **Entry fees.** One entry per member per draw, at
  `FLYCONOMY_LOTTERY_TICKET_PRICE`. The money sits in the pot until it is won.
- **A rake on the casino's net winnings**, `FLYCONOMY_LOTTERY_RAKE`, defaulting
  to a quarter. The other three quarters are still destroyed, so the casino
  stays a money sink.

A draw runs every `FLYCONOMY_LOTTERY_DRAW_HOURS` and pays one entrant, picked
uniformly. With nobody entered the pot rolls over untouched, so a jackpot builds
on a quiet server. `$draw` runs one immediately.

**Odds cannot be bought.** Everyone entered has exactly one entry, enforced by a
primary key on `(draw, user)` rather than by application code.

**A win never shrinks the pot, and a loss always feeds it.** The rake is
computed per wager from what the house won on that one hand, signed so a
player win contributes nothing — but a win is never clawed back out of the pot
either, since `add_to_pot` ignores non-positive amounts. That is a deliberate
trade-off, not a closed loophole: because the rake reads the gross result of
each wager rather than a player's net result across many, a game with 0% edge
still feeds the pot on every loss. Churning coinflip at the table limit is
"free" in the sense that wins and losses cancel out for the player, but every
individual loss along the way still pays its share into the pot. It stays this
way anyway because protecting a winner's payout from being retroactively taken
back matters more than closing that narrow farming path — see
`FLYCONOMY_MAX_BET` and the shared rate limit for what actually bounds it.

The pot's share tracks how often a game loses outright, not its average edge,
so the two do not rank games the same way. At the default rake and an equal
$200M turnover on each:

| Game | Loses outright | Into the pot |
| --- | --- | --- |
| Coinflip, 0% edge | 50.0% | $25.00M |
| War, 0% edge | 47.1% | $23.53M |
| Roulette colour, 5.26% edge | 52.6% | $26.32M |
| Slots, 4.17% edge | 83.3% | $41.67M |

Slots has the smallest average edge here but feeds the pot the most, because
most spins are a total loss rather than a partial one. These are computed
exactly over each game's full outcome space (a coin toss, the 52-card deck,
the 216 reel combinations, the 38-pocket wheel), not sampled.

The pot is floored at zero, so a run of player wins cannot take it negative.

### Creator tax

A second, optional cut of the same house take that the lottery rake reads,
paid into a single configured member's *bank* balance instead of the pot.
Because it reads the same per-wager figure, it applies to every game's every
loss exactly like the table above — coinflip and war included, not just the
games with a listed house edge. It is carved out of the share the lottery
rake leaves for destruction, so turning it on does not change how much a loss
takes from the loser or how much the pot receives — it only redirects part of
what would otherwise be destroyed. It follows the same wins-contribute-nothing
rule as the lottery rake: a player win never pays it, and there is nothing to
claw back.

Off by default in the sense that matters: `FLYCONOMY_CREATOR_TAX_USER_ID` is
unset, so no account is credited regardless of `FLYCONOMY_CREATOR_TAX_RATE`.
Set the ID to turn it on.

### Blackjack

`blackjack <bet>` deals two cards to you and two to the dealer, one of them face
down, then posts **Hit**, **Stand**, and **Double Down** buttons. Only the member
who was dealt the hand can press them. If nobody acts within 90 seconds, the hand
stands automatically and still pays out, so a stake is never stranded.

House rules, all of which are the player-friendly variants:

| Rule | This table |
| --- | --- |
| Dealer on 17 | Stands on every 17, soft ones included |
| Natural blackjack | Pays 3:2 |
| Double down | Allowed on the opening two cards only, for one more card |
| Tie | Pushes, and your stake is returned |
| Splitting | Not offered |

Doubling down debits a second stake equal to your first. If your wallet cannot
cover it, the hand is left untouched rather than dealt a free card.

Blackjack is the only game here whose house edge depends on how you play, so
there is no single figure to quote. Measured over 300,000 hands against this
ruleset:

| How you play | House edge |
| --- | --- |
| Stand on everything | 15.8% |
| Copy the dealer, hitting below 17 | 6.1% |
| Simple basic strategy | 3.1% |
| Simple basic strategy, doubling on 9 to 11 | 1.4% |

Played well, blackjack is the best bet in the casino. Played badly, it is the
worst. That is the point of it.

### Crash

`crash <bet>` starts a multiplier climbing from 1.00x and posts a **Cash Out**
button. Press it before the multiplier crashes to lock in that payout; wait
too long and the stake is gone. Only the member who staked it can press the
button. If nobody acts, the round settles as a loss once the crash point is
reached, so a stake is never stranded.

House rules:

| Rule | This table |
| --- | --- |
| Growth rate | 1.06x per second |
| Maximum multiplier | 20x, which caps a round at about 51 seconds |
| Live redraw | Every 2 seconds, best effort |
| Decision timeout | 75 seconds, a safety net well past the longest possible round |

Unlike blackjack, crash's house edge does not depend on how you play: cashing
out at any fixed target multiplier has the same expected profit, a flat **3%**
of the stake. That is a property of the formula the crash point is drawn from,
not an average over strategies — see `crash.Game.deal`'s docstring for the
derivation, and `tests/test_crash.py` for the simulation that checks the
sampler actually matches it.

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
| `test_antiabuse.py` | That no game is profitable, faucets stay throttled, and the limits hold. |
| `test_ratelimit.py` | The sliding window limiter, on an injected clock. |
| `test_season.py` | That a 365-day season stays bounded and grows linearly. |
| `test_lottery.py` | The pot, entries, draws, and the rake. |
| `test_blackjack.py` | The blackjack ruleset: hand values, soft aces, dealer policy, payouts. |
| `test_crash.py` | The crash ruleset: the multiplier curve, the crash-point sampler, and its house edge. |
| `test_views.py` | The blackjack and crash buttons, ownership, timeout, and settlement. |
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
├── blackjack.py      The blackjack ruleset, also free of any discord import
├── crash.py          The crash ruleset, also free of any discord import
├── economy.py        Every tunable number and the pure rules that use them
├── embeds.py         Message and embed builders
├── errors.py         Exceptions the bot raises deliberately
├── logging_config.py Logging setup
├── ratelimit.py      A sliding window limiter, with an injectable clock
├── views.py          Interactive buttons: the blackjack table and the crash round
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

### `docker compose` fails with "unknown shorthand flag: 'd' in -d"

The Docker Engine is installed but the Compose v2 plugin is not, so `docker`
tries to parse `compose up -d --build` as flags on itself instead of running
Compose. Run `./scripts/deploy.sh`, which installs the plugin, or install
`docker-compose-plugin` yourself following
[Docker's install guide](https://docs.docker.com/compose/install/linux/).

### `apt-get install docker-compose-plugin` says "Unable to locate package"

Your distro's own repos never carried `docker-compose-plugin` — it's only
published in Docker's official apt repo, which isn't configured on your host.
This is common on an end-of-life release (Ubuntu 23.04/lunar and older, whose
sources have moved to `old-releases.ubuntu.com`). `./scripts/deploy.sh`
detects this and falls back to installing the Compose plugin binary directly
from [Docker's GitHub releases](https://github.com/docker/compose/releases),
no apt repo required. To do it by hand instead, download the
`docker-compose-linux-<arch>` binary for your architecture and save it as
`~/.docker/cli-plugins/docker-compose` (or `/usr/local/lib/docker/cli-plugins/docker-compose`
as root), then `chmod +x` it.

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
| Rock paper scissors no longer refunds ties. | Refunding the tie paid players +33% of everything staked on the game, which no rate limit could close. It is now 0%, like coinflip, dice, and war. |
| `beg` moved from a 3-second to a 60-second cooldown. | At 3 seconds it created about $30,000 an hour from nothing, more than a maximum-level miner produced. |
| Wagers are capped, and game commands share a rate limit. | See [Keeping the economy honest](#keeping-the-economy-honest). |
| `daily` is capped at $10,000 a claim. | It paid 10% of the bank, compounding, which is a factor of 1.28e15 over a year. It was the only source that compounded, and the only thing standing between the bot and hyperinflation. |
| A lottery was added. | Gives the casino's winnings somewhere to go besides deletion, without creating money. See [The lottery](#the-lottery). |
| Three games were added: `blackjack`, `slots`, and `war`. | The casino had no game of skill, no jackpot game, and no game with a push. All three are documented in the payout tables above. |
| Mining odds at levels 2 through 5 are now 5%, 10%, 15%, and 20%. | Version 1 tested `randint(1, 100) in range(1, 5)`, which is 4%, not the 5% it announced. Every level was short by one point. The odds now match what the bot has always claimed. |
| Roulette has a real `00` pocket. | Python reads the literal `00` as `0`, so version 1's wheel held two `0` pockets and no `00`. Betting on `0` paid at double the correct rate. |
| Negative bets are rejected. | Version 1 compared `bet > wallet` and then subtracted the bet, so a negative bet added money to the wallet. |
| Robbing yourself is refused. | Version 1 announced the refusal and then robbed you anyway. |
| A losing balance can never go negative. | Balance checks are now part of the SQL update instead of a separate read. |
| Owner commands are prefix-only. | A slash command is published to every member, including the ones who can't run it. |
| The token is read from the environment. | Version 1 kept the token in the source file. |

Version 1 is preserved in git history at commit `8e9226e`.
