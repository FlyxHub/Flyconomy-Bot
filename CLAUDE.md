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

## Workflow

Commit and push after every change, without waiting to be asked. Stage only the files the change
actually touched, write a message that follows the existing commit style (short, present tense,
explains the why), and push straight to `main` — this repo has no branch-protection or review step
that would make that unsafe.

## Architecture

`src/flyconomy/` — `__main__` (entry point) → `bot.py` (client) → `cogs/` (commands) over
`database.py`, with `economy.py` and `blackjack.py` holding the rules, `views.py` the interactive
buttons, `ratelimit.py` the abuse throttle, `guide.py` the member guide's text, and `config.py`
the settings. The lottery adds two
tables in migration 3, the jackpot two more in migration 5, head-to-head matches one in
migration 6, wallet security one in migration 7, the published guide one in migration 8,
self-reset history one in migration 9, the daily interest ledger one in migration 10, and the
market's regime plus the daily Flyxcoin buying ledger one in migration 11; the
`bank` table is still untouched. A
member with no `security` row is level 0, so that migration writes no rows at all — a per-member
level that defaults to zero needs a table and a `LEFT JOIN`, not a sixth column on `bank`.
Head-to-head games share `MatchView` (escrow settlement) and `MatchChallengeView` (the offer) in
`views.py`, which are deliberately game-blind: a second one is a rules module, a board view, and
a command. Tic-tac-toe is the only one right
now, so `tests/test_matches.py` covers that shared half against it.

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

## The member guide

`data/economy-guide.md` inside the package is the guide members read, and `cogs/guide.py`
publishes it: posted to `settings.guide_channel_id` on startup, then edited in place whenever the
text changes. Four things about it are load-bearing.

**It ships as package data, not in `docs/`.** The Dockerfile copies only `pyproject.toml`,
`README.md`, and `src/`, and installs the wheel — a guide kept in `docs/` could be edited forever
without the running bot ever seeing it. This bit once: `.gitignore` had an unanchored `data/` for
the runtime database directory, which also matched `src/flyconomy/data/`, and because hatchling
honours VCS ignore rules the guide was silently dropped from the wheel while every test passed.
The rule is anchored to `/data/` now. Verify a packaging change by building the wheel and looking
inside it, because no test can see this.

**An unchanged section costs no API call.** Each posted message is stored with a checksum of its
text in the `guide` table, so a restart that finds the guide current does nothing at all. Without
that, a crash-restart loop would rewrite the channel on every boot.

**Order beats efficiency.** A new Discord message lands at the bottom of the channel, so anything
that would leave the sections out of reading order — a section added or removed, a message
deleted, the channel changed — reposts the guide whole rather than patching the one that differs.
`_is_reusable` is the single place that decision is made; editing in place is only safe when the
stored rows line up exactly with the sections that exist now.

**The guide is prose, so only a test stops it lying.** `tests/test_guide.py` fails if a member
command exists that the guide never names, or if a tuned figure (security and miner prices, max
bet, daily cap, ticket price, starting bank, transfer tax) no longer appears in the text. Retuning
the economy therefore fails the build until the guide is retuned too, which is the point — a stale
guide tells members the wrong odds and nothing else notices. Adding a member-facing command means
adding it to the guide in the same commit.

## Conventions

- **Adding a command:** put it in the cog it belongs to, decorate with `@commands.hybrid_command`,
  and give it a docstring — the docstring becomes both the `$help` text and the slash description.
  Annotate numeric arguments with `commands.Range[int, 1]` so Discord rejects bad input client-side
  and negative bets stay impossible.
- **Owner commands stay prefix-only** (`@commands.command` in `cogs/admin.py`). A slash command is
  published to every member, including those who can't run it. `$sync` republishes the tree. They
  all work in a DM with the bot, which needs nothing special — no command in that cog carries a
  guild-only check, and `is_owner` takes a `User` as happily as a `Member`.
  `$market bull|bear|neutral` is additionally `hidden`, so it is absent from `$help` even for the
  owner: the rest of the cog is merely uninteresting to a member, while knowing the market can be
  steered changes how a run reads. It arms `economy.start_run` and moves no price itself, so a triggered run is drawn from
  the same length distribution and plays out through the same tick as a spontaneous one, and the
  creator's run DM does not fire for it. `neutral` is `economy.end_run`, and moves no price
  either: it clears the regime and leaves the calm tick's snapback to haul the price home over
  the two or three ticks `economy.flx_snapback_ticks` counts, which is the same walk home a run
  that reached its goal takes. Putting the price back by hand would be a jump no tick can produce,
  and a cancelled run has to be indistinguishable from a finished one for the same reason a
  triggered one has to be indistinguishable from a spontaneous one. That command is only safe
  because `FLX_DAILY_BUY_CAP` exists — crash, buy, pump, sell is worth $489M over a season with
  the cap and $4.09 quadrillion without it. Don't keep one without the other.
- **`self.rng`** on `BaseCog` is the random source for game outcomes, so tests can seed it.
- **Interactive components** live in `views.py`. Keep the button callbacks trivial: each one calls an
  `apply_*` coroutine that takes no `Interaction`, then redraws. **Discord caps an action row at
  five components**, and a select menu fills a row on its own — this is a hard API limit, not a
  discord.py one, and dropping the embed does not change it. Design a board to that number rather
  than against it: tic-tac-toe's nine squares are three rows of three, where the wrap *is* the
  grid. A Connect 4 was built here and then removed over exactly this — seven columns wrapped two
  buttons onto a second row, and narrowing the board to five to fit made it a worse game than the
  one people expected. Check a board's controls against the five before designing the board. That split is what lets
  `tests/test_views.py` drive a whole hand against a real database with no gateway. A view that
  moves money must be idempotent — a click and a timeout can both reach it, so `BlackjackView.settle`
  guards with a `_settled` flag.
- Each cog decorator carries `# type: ignore[arg-type]`: discord.py's hybrid decorators are typed for
  pyright and mypy infers `Never` parameters. The ignores are narrow on purpose — mypy runs strict.

## The season

The economy runs a calendar year and is reset each January, so every balance
question is really "does this stay readable for 365 days".

**Only one thing ever compounded, and it is capped.** The daily interest pays a percentage of the
bank; at 10% a day that is 1.28e15 over a year. `DAILY_PAYOUT_CAP` (and `settings.max_daily_payout`)
bounds one account's payout for one day, which leaves the rate untouched while the bank is small and
turns growth into a straight line above that. Every other source — begging, mining, starting funds —
is linear and cannot run away inside a fixed season.

**The daily payout is a scheduled job, not a command.** It runs at
`settings.daily_payout_time` in `settings.timezone` and pays *every row in `bank`*, so nobody claims
it and nobody misses it. Three things are load-bearing. Idempotency is enforced by the primary key
on `daily_payouts(day)` rather than by a check in Python: `Database.pay_daily_interest` claims the
day and moves the money in one transaction, so a restart seconds after the tick cannot pay twice —
which is a real tightening, because the `commands.cooldown` it replaced lived in memory and every
restart handed everyone a fresh claim. The amount still comes from `economy.daily_payout`, never
from SQL: a bulk `SET bank = bank + MIN(bank / 10, cap)` would fork the rounding rule somewhere no
unit test can reach it. And the startup catch-up in `_before_interest_loop` backfills *today only*,
and only when the scheduled time has already passed — backfilling every missed day would turn a
week-long outage into a week of interest paid at once, which is the one shape the linear-growth
bound does not survive.

Because nobody claims it, `_announce_interest` posts the run to
`settings.lottery_announce_channel_id` — the lottery's channel, shared rather than given a second
setting, since it is the same "things the bot did on its own" feed. It carries the lottery's
contract with it: the money has already moved when the post is attempted, so it goes through
`BaseCog.resolve_channel` and an unreachable channel is logged rather than raised. A run that
credited nobody is not announced, or a quiet server gets "$0 across 0 accounts" every morning.

**Automatic payment changed who collects, not how much.** The cap is still per account per day, but
issuance now scales with the number of rows in `bank` rather than with how many members showed up,
and a row lasts forever. That is linear in accounts and capped per account, so it holds inside a
season — `tests/test_season.py` pins both halves, including a 500-account server nobody plays on.
The accepted cost is that an untouched account still grows all year, which makes a seeded second
account a straight multiplier on the only faucet that compounds, needing no attention after the day
it is seeded. That is a Discord-level problem, not an economy-level one.

**A sink is the safe direction.** `secure` is the counterweight added when robbery was driving
people away from the games: it buys down the odds a `rob` against you lands, costs bank money,
and returns nothing. Because it only ever destroys money it needs no cap, which is why a
defensive upgrade is a far cheaper thing to add than another faucet.

**A redistribution is the next safest, and `pay` is one, not a sink.** Its tax is paid back into the
economy rather than destroyed, so it does not shrink the supply the way `secure` does. It needs no
cap for a different reason than `secure` does: a transfer cannot create money either. Don't file it
under sinks.

**Before adding any income, ask whether it is a percentage of something that grows.** If it is, it
compounds, and it needs a cap. That single question is what `tests/test_season.py` exists to enforce:
it plays a full 365-day season on every commit and fails if the supply or the richest member leaves
sane bounds, or if growth stops looking linear.

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
- **A reset is a faucet, and was the largest one.** `resetme` seeds a fresh account, so it belongs in
  the paragraph above rather than under "destructive commands". Ungated it paid `STARTING_BANK` per
  invocation with nothing but the shared rate limit in the way — about $2.1M an hour, which is what
  made "gamble everything, then start over" a strategy. It is deliberately still *never refused*:
  the bound is the payout, not a gate, because a member who has lost everything should meet a worse
  deal rather than an error message. Resets less than `RESET_CYCLE_SECONDS` apart form one chain and
  each seeds half the last (`economy.reset_seed`), so the chain is a convergent series — $1,750
  total, under a day of begging and under one capped `daily`, however many resets are in it. That
  sum, not any per-call limit, is what `tests/test_antiabuse.py` pins; a new schedule has to keep
  the series convergent. Three things are load-bearing. The chain is measured from the *last* reset,
  so it cannot be kept alive cheaply and then cashed out on the original clock.
  `Database.reset_account` writes the seeded `bank` row itself instead of leaving it to
  `ensure_account`, which would hand back the full starting bank on the member's next command. And
  the `resets` row is the one thing a self-reset does not delete, since a counter the reset clears
  always reads zero — it stores the *current chain length*, not a lifetime total, and
  `resets_in_cycle` applies the expiry on read because the stored row is stale until the next reset
  rewrites it. `purge_user` does clear it — that is the staff path, and a moderator undoing
  something is not a member working the schedule.
- **A shared rate limit,** in `BaseCog.cog_check` over `ratelimit.SlidingWindowLimiter`. Deliberately
  *not* per-command: a per-command cooldown is dodged by rotating between games, and cannot cover
  commands that refund their own cooldown when they decline to act (`mine` without a miner, `rob` on
  an empty wallet), which would otherwise loop for free.
- **The lottery pot only ever grows.** `BaseCog.rake` is handed the signed `stake - returned` on
  every wager, but `Database.add_to_pot` ignores non-positive amounts, so a
  player win contributes nothing and is never clawed back out of the pot — a lost hand can no longer
  cancel out an earlier win elsewhere. This is a deliberate, accepted departure from the stricter
  "net take" design: gross losses on a fair game are in principle farmable by churning it, since the
  house's *net* take from a fair game is zero but its *gross* rake from losses alone is not. It's kept
  this way anyway because member-facing pot integrity (a win should never shrink the jackpot) outweighs
  that narrow farming risk. Every game calls `Gambling._settle` exactly once per wager, *including on
  a loss with a multiplier of zero*, so the rake sees wins and losses both. Blackjack settles in
  `BlackjackView.settle` instead and rakes there. One entry per member per draw is enforced by a
  primary key, not by application code. The casino is no longer the pot's only source — half of
  every `pay` transfer's tax feeds it too — but the property that matters is unchanged: nothing
  ever takes money back out except a draw.
- **A table limit,** `settings.max_bet`, enforced in `Gambling._stake`. Every wager debits through
  that one method, so a new game cannot forget the cap. Check the limit *before* debiting, so a
  refused bet costs nothing. The jackpot is the one wager that cannot use `_stake`, because its
  ante and its entry have to move in one transaction; it calls `_check_limit` directly instead, and
  its Join button antes the opener's already-checked amount rather than taking a new one.
- **Player-funded pots pay out less than they take in.** The jackpot has no house bankroll behind
  it: the pot is only ever the sum of its entries, and the winner takes it less
  `jackpot.HOUSE_CUT`, so a round can only shrink the supply. Weighting the odds by ante is safe
  *here* precisely because the entrants fund the pot themselves — the lottery forbids buying odds
  because its pot is fed by the house's rake instead. Don't carry either rule across to the other.
- **Money held across a live match always has a way back.** A head-to-head wager outlives the
  command that placed it: both stakes go into the `escrow` table when the challenge is accepted,
  and the board that decides them lives in a view in memory. Every way that view can end — a win, a
  draw, a resignation, a move timeout — routes through `MatchView.settle`, and the two ways it can
  *stop existing* are covered too: `Gambling.cog_load` refunds every hold at startup, and
  `purge_user` voids a match and refunds the opponent. A new game that holds money across turns
  needs all four paths, not three, which is why the settlement lives on the base class rather than
  in each game. Nothing is staked while a challenge is merely offered, which is what makes an
  unanswered or declined challenge cost nothing. An open challenge — one with no named opponent —
  can be pressed by several members at once, so accepting is serialized behind a lock in the view:
  without it two presses could both pass the "still open" check while the first was still awaiting
  its escrow, and the challenger would be staked twice for one seat.
- **A drawn game must not make the wager pointless.** Tic-tac-toe draws every time between players
  paying attention, so a single board would refund almost every match. It plays best of three with
  the first move swapping each board, and only a match where nobody slips three times over is
  called off. Board counts stay odd so the first-move advantage is shared as evenly as it can be.
  Before adding a game with a common draw, work out how often two competent members would actually
  move money — a game that mostly refunds is a game nobody plays twice.
- **A transfer redistributes; it must never mint.** `pay` is the one taxed flow whose tax is *not*
  destroyed: half goes to the lottery pot and half to `creator_tax_user_id` (destroyed while that
  is unset). That is only safe because a transfer creates nothing on the way in, so the supply can
  fall but never rise. The exploit to check for is a colluding pair, and the bound is that the most
  they can recover is the creator's half — so every pass costs them at least the pot's half, which
  `tests/test_antiabuse.py` pins for the worst case where one of the pair *is* the creator. All
  four legs (debit, credit, pot, creator) settle in one `_transaction()`, so the tax cannot be
  collected on a transfer that was refused. Note what the tax does not buy: it is not a brake on
  funnelling money into an alt, because `flx send` was already free before `pay` existed —
  `flx_cost` quotes one price to buyer and seller alike, so a round trip through coins costs
  nothing. Any rate above zero sends large transfers down that rail; what actually splits the two
  rails is that coins move in whole units, so Flyxcoin cannot carry less than one coin's price.
  Raising the rate steers nothing — reprice it only to change what the small rail costs.
- **The market is the one thing here that multiplies, and only a cap bounds it.**
  Every game is held in check by having no positive expected value. The market
  cannot be: the walk mean-reverts to a fixed anchor and `flx_cost` quotes one
  price to buyer and seller, so buying low and selling high is a round trip that
  completes on its own and pays a *percentage of the member's whole bank*. That
  is the compounding shape `DAILY_PAYOUT_CAP` exists to stop, and before
  `FLX_DAILY_BUY_CAP` nothing was stopping it — a season of casual band trading
  turned $100k into $84 trillion, and no test could see it because
  `tests/test_season.py` modelled no trading at all. It does now, and
  `TestTheMarketIsBoundedToo` fails if the cap stops being what holds the season
  together. Three things are load-bearing. The cap is **in coins, not dollars**,
  so a day's gain is bounded in absolute dollars rather than as a share of a
  balance, which is what makes the season linear; a dollar cap would let a rich
  member buy the same *value* every day and compound anyway. Only **buying** is
  capped — selling is bounded already by what a member bought or mined, and
  selling must not refund the day's allowance or churning would dodge the cap
  entirely. And `flx_purchases` survives `reset_account` while `purge_user`
  clears it, exactly like the `resets` row: a limit a member can clear by
  resetting is not a limit.
- **Nothing member-facing names the regime.** `flx info` once carried a `Market: Bull run` field,
  which meant a member never had to notice a run — the one thing the market asks of them.
  `embeds.circulation_embed` takes the price rather than the whole `MarketState` so the regime
  cannot leak back in, and `tests/test_bot.py` fails if that embed ever says "bull", "bear", or
  "run" again. The status ticker's price and last move is the tell members are meant to read; the
  creator's run DM is the one exception and is a perk, not a mechanic.
- **A run needs state; a wider shock is not a run.** `next_flx_market` takes and
  returns a whole `MarketState` because mean reversion erases a one-tick spike
  within the hour — raising `FLX_VOLATILITY_PERCENT` gives a noisier flat line,
  never a trend. Reversion is *suppressed but not zero* during a run, so the
  trend meets a pull that grows with distance and the run decelerates under its
  own weight; that is why a run needs no separate size cap and why the bounds
  are a backstop rather than the mechanism. Set it to zero and a run rides the
  bound for its whole length.
- **A run's trend is a sign bias, not a drift.** The first version added a
  steady `FLX_RUN_DRIFT_PERCENT` under a symmetric shock, and the arithmetic of
  that is unavoidable: a drift small enough not to pin the ceiling still leaves
  roughly a third of a bull run's ticks red, so it read as a choppy market that
  happened to end higher. `FLX_RUN_WITH_TREND_PERCENT` biases *which way the
  shock points* instead (85% with the run), and the magnitude is drawn
  `triangular` with its mode at the maximum so the moves that land are mostly
  large ones. Down-ticks fell from 37% to 16% and the median run tick roughly
  doubled. Three things are load-bearing. The bias may never reach 100 — a run
  that cannot tick against itself is free money, because timing the exit stops
  being a decision. The trend engine and its brake scale together, so
  `FLX_RUN_REVERSION_PERCENT` went 1 → 4 and the run got *shorter* (8–24 ticks,
  from 12–36); a stronger trend on the old brake and the old length just pins
  the ceiling, which is the one failure the deceleration argument above does
  not survive. And the brake stays below the calm pull, or "suppressed during a
  run" stops being true — `tests/test_economy.py` pins that, the down-tick
  share, the large-move share, and the share of runs that reach a bound.
  Retuning the shock means re-checking all four, and re-describing the run in
  the guide, which quotes both the tick size and the bias.
- **A run ends by arriving, not by running out of ticks.** `flx_run_goal` is
  what makes a run land *near* a bound. Nothing else does, and no amount of
  tuning substitutes: the shock bias points the price at an equilibrium around
  $100,000, so inside the band a bull tick is still worth about +3% at $18,000
  and neither the suppressed reversion nor the clamp is bending it. On ticks
  alone a run's peak was just a function of the length it drew — a median peak
  of $15,800, one in seven anywhere near the ceiling, and one in eleven sat
  clamped against $20,000 for a median 12% of its length. With the goal, 84%
  peak between $18,000 and $20,000 and none reach the bound.
  `FLX_RUN_TARGET_PERCENT` is set to 80 rather than nearer 100 for a reason
  worth keeping: a run stops *at* $18,000, so the largest tick that can follow
  lands at $19,440 and the clamp is unreachable by arithmetic rather than by
  luck — `test_a_run_cannot_reach_its_bound_by_arithmetic` is that proof, and
  raising either the goal or `FLX_RUN_VOLATILITY_PERCENT` without re-checking
  it puts runs back on the ceiling. The tick deadline stayed, demoted to a
  backstop that fires for about one run in six; that minority is the only thing
  keeping a run from being a quantity a member can price in advance, so don't
  narrow `FLX_RUN_MIN_TICKS`/`FLX_RUN_MAX_TICKS` to make runs more consistent.
  Both directions run off the same goal and the same snapback, and
  `tests/test_economy.py` parametrises every run property over the two rather
  than checking bull and trusting the symmetry — the band is lopsided ($10,000
  of room above the anchor, $5,000 below), so symmetric code does not by itself
  give symmetric behaviour. It happens to here: a bear lands at $5,850 against
  its $5,000 floor and comes home in two ticks. The one asymmetry left is that
  a bear reaches its goal 94% of the time against a bull's 83%, because the
  same shock covers the shorter distance to $6,000 in fewer ticks; that is why
  `test_some_runs_still_fall_short` is pinned at the low bar and not the
  measured one.
  The season figure is unmoved at $65M against a $10B ceiling either way:
  `FLX_DAILY_BUY_CAP` and the band bound the market, not the shape of a run.
- **The calm pull is a threshold, not one rate.** `FLX_MEAN_REVERSION_PERCENT`
  (5%) applies inside `FLX_CALM_BAND_PERCENT` of the anchor and
  `FLX_SNAPBACK_REVERSION_PERCENT` (60%) outside it. The second exists because
  a run's aftermath was longer than the run: the gentle pull took a median 37
  ticks -- three hours -- to give back a climb that took one, so the shape a
  member actually saw was a spike followed by an afternoon of sag they could
  buy into. It is three ticks now, and a run reads as a spike. The obvious
  alternative is a pull that ramps with distance, and it does not work: a ramp
  is weakest over the last and slowest stretch, so even a steep one still took
  seven ticks. A threshold is also why this is invisible in normal play -- a
  quiet market is inside the band 98% of ticks and never feels it, which
  `tests/test_economy.py` pins alongside the time home. Note the side effect
  worth keeping in mind: the calm band is now genuinely tight, so buying a dip
  between runs is close to pointless, and a run is the only time the market is
  worth trading. That is the intended shape, not an accident. The creator's run DM rides on
  `creator_tax_user_id` and is a perk, not a mechanic: the run is stored before
  the DM is attempted, and the cap above is what keeps the information from
  being worth anything.
- **A defense may never become an immunity.** Wallet security lowers `rob`'s success rate and
  nothing else — the top level still lets one robbery in ten through, and `rob_success_percent`
  clamps a level off the end of the table back onto it. A wallet that cannot be robbed removes
  the reason to `deposit` at all, and casino stakes come out of the wallet, so the wallet has to
  stay the risky place to keep money. Price a defense so it is a season-long goal (the whole
  track is over a month of capped dailies, which `tests/test_antiabuse.py` asserts) and never
  let one pay anything back.

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
