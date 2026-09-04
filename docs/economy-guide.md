Post this in your server as six messages, in order. Discord caps a message at
2,000 characters, so each block below is sized to fit on its own. Copy
everything between the separator lines; don't paste the separator lines
themselves.

Every number here comes from `README.md`. If you retune the economy, retune
this too — a guide that lies about the odds is worse than no guide.

════════════════════════════════════ 1 of 6 ════════════════════════════════════

# 💵 The Flyconomy

Everything here is play money. Nothing costs a real cent, nobody can spend
anything you own, and the whole economy is wiped and restarted every January.

Every command works two ways — `/balance` or `$balance`. Slash commands give
you argument hints, so start there.

## Your first five minutes

You begin with **$1,000 in the bank** and an empty wallet.

1. `/daily` — pays 10% of your bank, up to $10,000. Once every 24 hours. Never skip it.
2. `/beg` — pays $1 to $100, about half the time. 60-second cooldown.
3. `/withdraw 500` — move cash to your wallet. The casino only stakes from there.
4. `/coinflip heads 100` — you're playing.
5. `/deposit` — put the rest back somewhere safe. The next message explains why.

`/balance` shows everything you own. `/leaderboard` shows who's beating you.

-# Stuck? `$help` lists every command. `/resetme` wipes your account back to a new player, if you ever want to start over.

════════════════════════════════════ 2 of 6 ════════════════════════════════════

# 🏦 Wallet vs. bank

**Wallet cash can be stolen. Banked cash cannot.** That one rule drives
everything else.

Every wager is staked from your wallet, and every payout lands back in it. So
playing at all means leaving money where `/rob` can reach it.

- `/deposit` — wallet ➜ bank. Do this before you log off.
- `/withdraw` — bank ➜ wallet.
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

════════════════════════════════════ 3 of 6 ════════════════════════════════════

# ⛏️ Flyxcoin

The server's fake cryptocurrency. It is a real second asset — its price moves
on its own, and your coins count toward your net worth at whatever it's worth
right now.

## Mining

`/upgrade` buys your miner and levels it up, paid from your bank. `/mine` then
runs once an hour.

```
Level   Costs to reach   Chance to mine a coin
  1     $100             1%
  2     $5,000           5%
  3     $20,000          10%
  4     $100,000         15%
  5     $500,000         20%
```

One coin is worth roughly $10,000, so a maxed miner averages about $2,000 an
hour for free. It pays for itself, slowly, and it is the only income in the
game that keeps earning while you sleep.

## The market

- `/flx info` — the live price, the total in circulation, and what yours is worth. As a prefix command, plain `$flx` does the same.
- `/flx buy [amount]` — buys at the current price, from your bank.
- `/flx sell [amount]` — sells at the current price, into your bank.
- `/flx send @someone <amount>` — hand coins to another member.

The price starts at $10,000 and moves every 5 minutes, up to 3% at a time. It
is pulled gently back toward $10,000 and can never leave the $5,000–$20,000
band, so it swings but it cannot moon and cannot go to zero. Buy the dips if
you like — just know the floor and ceiling are real.

-# Flyxcoin can't be robbed — only cash can. Coins are the safest place to park a fortune, as long as you can stomach the price moving.

════════════════════════════════════ 4 of 6 ════════════════════════════════════

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

════════════════════════════════════ 5 of 6 ════════════════════════════════════

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

- `/lottery info` — the current pot and how many are in. Plain `$lottery` works too.
- `/lottery enter` — buy your ticket.
- `/lottery entrants` — who's in.

The pot is fed by ticket sales *and* by a quarter of everything the casino
wins. Nobody enters, nobody wins, and the pot rolls over — so a quiet week
builds something worth showing up for.

════════════════════════════════════ 6 of 6 ════════════════════════════════════

# 📋 House rules & cheat sheet

- **Maximum bet:** $100,000 a wager. A bet over the limit is refused outright and costs you nothing.
- **Rate limit:** six game commands per 10 seconds, shared across every game. Spamming is throttled, not punished.
- **Cooldowns:** `beg` 60 seconds · `mine` and `rob` 1 hour · `daily` 24 hours.
- **Balances can't go negative.** A bet you can't cover is refused, not overdrawn.
- **Nothing is stranded.** Every button — blackjack, crash, tic-tac-toe — pays out or refunds on its own if you walk away mid-hand.
- **The season resets each January.** Everything you build is for the year.

```
BANKING     /balance  /deposit  /withdraw  /leaderboard  /wallets
INCOME      /daily  /beg  /mine  /upgrade
DEFENCE     /secure
ROBBERY     /rob @member
FLYXCOIN    /flx info  /flx buy  /flx sell  /flx send
CASINO      /coinflip  /dice  /rps  /roulette  /slots  /war
            /blackjack  /crash
TOGETHER    /jackpot  /tictactoe  /lottery info  /lottery enter
```

Two habits separate the people at the top of `/leaderboard` from everyone else:
they claim `/daily` every single day, and they never leave money in their
wallet they aren't actively gambling.

Good luck. 💸

-# Every command in this guide works as `/name` or `$name`. The one exception is `$help`, which is prefix-only.
