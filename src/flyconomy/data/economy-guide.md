The bot publishes this file itself. Set `FLYCONOMY_GUIDE_CHANNEL_ID` and it
posts these sections to that channel on startup, then edits those same messages
in place whenever this file changes — so editing here and redeploying is the
whole workflow. Nothing above the first separator line is ever posted.

Each block between the separator lines becomes one Discord message and has to
stay under 2,000 characters; `tests/test_guide.py` fails the build if one
doesn't. Section numbering in the separators is decorative — the bot goes by
order, not by the numbers, so renumbering by hand is never required.

Every number here comes from `README.md`. If you retune the economy, retune
this too — a guide that lies about the odds is worse than no guide.

════════════════════════════════════ 1 of 7 ════════════════════════════════════

# 💵 The Flyconomy

Everything here is play money. Nothing costs a real cent, nobody can spend
anything you own, and the whole economy is wiped and restarted every January.

Every command works two ways — `/balance` or `$balance`. Slash commands give
you argument hints, so start there.

## Your first five minutes

You begin with **$1,000 in the bank** and an empty wallet.

1. `/beg` — pays $1 to $100, about half the time. 60-second cooldown.
2. `/withdraw 500` — move cash to your wallet. The casino only stakes from there.
3. `/coinflip heads 100` — you're playing.
4. `/deposit` — put the rest back somewhere safe. The next message explains why.

**Your bank pays interest every morning at 8:00 AM.** It's 10% of your bank
balance, up to $10,000 a day, and it lands on its own — there's nothing to
claim and nothing to remember. Money in your wallet earns nothing, so the
bank is where it grows.

`/balance` shows everything you own. `/leaderboard` shows who's beating you.

-# Stuck? `$help` lists every command. `/resetme` starts you over from nothing — read the house rules before you reach for it.

════════════════════════════════════ 2 of 7 ════════════════════════════════════

# 🏦 Wallet vs. bank

**Wallet cash can be stolen. Banked cash cannot.** That one rule drives
everything else.

Every wager is staked from your wallet, and every payout lands back in it. So
playing at all means leaving money where `/rob` can reach it.

- `/deposit` — wallet ➜ bank. Do this before you log off.
- `/withdraw` — bank ➜ wallet.
- `/pay @someone <amount>` — sends bank money to another member, minus a 5% tax. $100 minimum. Alias `/transfer`.
- `/rob @someone` — takes a random slice of their wallet. One attempt per hour.
- `/wallets` — the ten fattest unbanked wallets in the server. It is a target list, and you are on it.

## 🔐 New: wallet security

`/secure` buys a level of protection, paid out of your bank. Each level makes a
robbery against you **less likely to land**.

```
Level   Costs to reach   A robbery on you succeeds
  0     —                50% of the time
  1     $2,500           40% of the time
  2     $15,000          30% of the time
  3     $60,000          22% of the time
  4     $250,000         15% of the time
  5     $1,000,000       10% of the time
```

Four things worth knowing:

- It changes **how often** a robbery works, never how much a successful one takes.
- Level 5 is not immunity. One attempt in ten still gets through, so bank anything you aren't about to gamble.
- The money is spent, not stored. Security is the only upgrade in the bot that pays nothing back.
- The full track costs $1,327,500. Treat it as a season-long project.

Your level shows on `/balance`, right next to your miner.

════════════════════════════════════ 3 of 7 ════════════════════════════════════

# ⛏️ Flyxcoin

The server's fake cryptocurrency — a real second asset whose price moves on its
own, counting toward your net worth at whatever it's worth right now.

## Mining

`/upgrade` buys and levels up your miner, paid from your bank. `/mine` then runs
once an hour.

```
Level   Costs to reach   Chance to mine a coin
  1     $100             1%
  2     $5,000           5%
  3     $20,000          10%
  4     $100,000         15%
  5     $500,000         20%
```

One coin is worth roughly $10,000, so a maxed miner averages about $2,000 an
hour for free — the only income that keeps earning while you sleep.

## The market

- `/flx info` — the live price, the total in circulation, what it is all worth, and who holds the most. Plain `$flx` does the same.
- `/flx buy [amount]` — buys at the current price, from your bank.
- `/flx sell [amount]` — sells at the current price, into your bank.
- `/flx send @someone <amount>` — hand coins to another member.

The price moves every 5 minutes, drifting up to 3% a tick and pulled back toward
$10,000 — it almost always sits between $9,000 and $11,000.

Every couple of days it breaks into a **bull run** or a **bear run**: an hour or
so of hard movement one way, in lurches of up to 8% a tick, five ticks in six
going with the run.

A run climbs until it reaches about **$18,000** — or falls to **$6,000** for a
bear — and then stops. About one in six stops short, so a run is never a sure
thing. Nothing announces one: the price is the only tell, so watch it.

**A run is a spike, not a plateau.** The price is hauled home within about three
ticks of one ending — 15 minutes. Sell into a bull run before it turns; buy into
a bear while it is down. Sit through either and you end up where you started.

**You can buy at most 100 Flyxcoin a day**, resetting at midnight. Selling and
sending are unlimited.

-# Flyxcoin can't be robbed — only cash can. Coins are the safest place to park a fortune, if you can stomach the price moving.

════════════════════════════════════ 4 of 7 ════════════════════════════════════

# 💸 Sending money to people

- **`/pay` costs 5%.** The only option under about $10,000 — coins move in whole units, and one costs more.
- **`/flx send` is free.** Buy coins, send them, let them sell. Nothing is withheld.

Move anything large as Flyxcoin — the only cost is the price drifting between the
buy and the sell, and it drifts both ways. Only *buying* is capped, at 100 coins
a day, so a big position is something you build up over a few days rather than
in one go.

-# The tax on `/pay` is not destroyed: half of it feeds the lottery pot, so every transfer makes somebody's draw a little richer.

════════════════════════════════════ 5 of 7 ════════════════════════════════════

# 🎰 The casino

Every game stakes from your wallet. Maximum bet is **$100,000** a wager.

- `/coinflip heads 500` — call it, 2x. Alias `/cf`.
- `/dice 4 500` — call a face, 6x.
- `/rps rock 500` — 3x on a win. **The house takes ties.**
- `/roulette red 500` — 2x on a colour, 35x on a single pocket (0–36 or 00).
- `/slots 500` — three reels. Three of a kind pays 9x to 55x. Alias `/slot`.
- `/war 500` — high card wins 2x, a tie gives your stake back.
- `/blackjack 500` — buttons to hit, stand, or double down. Alias `/bj`.
- `/crash 500` — a multiplier climbs from 1.00x. Cash out before it crashes.

## What each game actually costs you

"House edge" is the share of every dollar staked that the bot keeps on average.

```
Game               Win chance      Pays        House edge
Coinflip           1 in 2          2x          0%
Dice               1 in 6          6x          0%
Rock paper scis.   1 in 3          3x          0%
War                47%, 6% tie     2x          0%
Blackjack          how you play    2x / 2.5x   1.4% – 15.8%
Crash              when you stop   your target 3%
Slots              1 in 6          2x – 55x    4.17%
Roulette, colour   18 in 38        2x          5.26%
Roulette, pocket   1 in 38         35x         7.89%
```

**No game in here has a positive expected value.** There is no grind, no
pattern, and no clever loop that prints money — that's checked by a test on
every build. The fair games are genuinely fair, and everything else quietly
favours the house.

Blackjack is the exception worth studying: played well it's the best bet in the
casino at 1.4%, and played badly it's the worst thing on the list at 15.8%.
Same game, same table, ten times the cost.

════════════════════════════════════ 6 of 7 ════════════════════════════════════

# 🏆 Playing against each other

Three games where the money comes from other members, not from the bot.

## `/jackpot <ante>`

Ante into a shared pot that stays open for **60 seconds**. Anyone can join with
their own ante, and a bigger ante buys a bigger share of the odds. One entrant
takes the whole pot, less a 5% cut. Alias `/jp`.

Everyone funds it themselves, so the pot is only ever what people put in — and
it pays out less than it takes. Fun, but it isn't a money source.

## `/tictactoe [@member] <bet>`

Challenge someone to **best of three** for matching stakes, or leave the offer
open and let anyone press Accept. Winner takes both stakes less a 5% cut.
Alias `/ttt`.

Nothing is staked while the offer sits there, so an ignored or declined
challenge costs nobody anything. Once it's accepted, both stakes are held until
the match ends. First move alternates each board, and only a match where all
three boards draw is called off with both stakes returned.

## `/lottery`

One ticket per member per draw, $10,000, drawn once a day. Every entrant has
exactly one entry — **odds cannot be bought here**, unlike the jackpot.

- `/lottery info` — the pot, everyone who's in, and the odds a ticket buys you. Plain `$lottery` works too.
- `/lottery enter` — buy your ticket.

The pot is fed by ticket sales *and* by a quarter of everything the casino
wins. Nobody enters, nobody wins, and the pot rolls over — so a quiet week
builds something worth showing up for.

════════════════════════════════════ 7 of 7 ════════════════════════════════════

# 📋 House rules & cheat sheet

- **Maximum bet:** $100,000 a wager. A bet over the limit is refused outright and costs you nothing.
- **Rate limit:** six game commands per 10 seconds, shared across every game. Spamming is throttled, not punished.
- **Cooldowns:** `beg` 60 seconds · `mine` and `rob` 1 hour.
- **Daily interest:** paid automatically at 8:00 AM to every account, 10% of your bank up to $10,000. You don't claim it, and you can't miss it.
- **`/resetme` is a last resort, not a comeback.** It deletes everything you own — cash, coins, miner, security — and seeds you a fresh account. Reset again straight away and you get less: $1,000, then $500, then $250, then nothing. Leave it 24 hours and you're back to the full $1,000. Gambling it all away and starting over is allowed; doing it four times in a day just leaves you with nothing.
- **Balances can't go negative.** A bet you can't cover is refused, not overdrawn.
- **Nothing is stranded.** Every button — blackjack, crash, tic-tac-toe — pays out or refunds on its own if you walk away mid-hand.
- **The season resets each January.** Everything you build is for the year.

```
BANKING     /balance  /deposit  /withdraw  /pay  /leaderboard  /wallets
INCOME      /beg  /mine  /upgrade  (bank interest is automatic)
DEFENCE     /secure
ROBBERY     /rob @member
FLYXCOIN    /flx info  /flx buy  /flx sell  /flx send
CASINO      /coinflip  /dice  /rps  /roulette  /slots  /war
            /blackjack  /crash
TOGETHER    /jackpot  /tictactoe  /lottery info  /lottery enter
```

One habit separates the people at the top of `/leaderboard` from everyone
else: they never leave money in their wallet they aren't actively gambling.
The bank pays interest and the wallet doesn't, and the wallet is the only
place `/rob` can reach.

Good luck. 💸

-# Every command in this guide works as `/name` or `$name`. The one exception is `$help`, which is prefix-only.
