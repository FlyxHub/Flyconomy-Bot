"""Pure economy rules.

This module holds every tunable number in the game and the pure functions that
act on them. It imports nothing from ``discord`` so the rules can be unit tested
without a gateway connection, and so that rebalancing the economy never requires
touching command-handling code.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

# --------------------------------------------------------------- currency ---

#: Price of one Flyxcoin, in dollars. Used for both buying and selling, and as
#: the multiplier that converts a Flyxcoin holding into net worth. Also the
#: anchor the random walk in :func:`next_flx_price` reverts toward.
FLX_PRICE: Final = 10_000

#: Bounds the live price random walk to, so a bad run of ticks can neither
#: collapse the price to nothing nor let it run away. The same kind of guard
#: as :data:`DAILY_PAYOUT_CAP` on the same kind of risk: something that could
#: otherwise compound.
FLX_PRICE_FLOOR: Final = FLX_PRICE // 2
FLX_PRICE_CEILING: Final = FLX_PRICE * 2

#: Largest single-tick move, as a percent of the price after mean reversion is
#: applied, in either direction. Applies while the market is calm; a run widens
#: it to :data:`FLX_RUN_VOLATILITY_PERCENT`.
FLX_VOLATILITY_PERCENT: Final = 3

#: Percent of the gap back to FLX_PRICE that each tick closes, before the
#: random shock is applied. Keeps the walk oscillating around the anchor
#: instead of drifting out to a bound and sitting there.
FLX_MEAN_REVERSION_PERCENT: Final = 5

#: One in this many calm ticks starts a bull or bear run. At
#: :data:`FLX_TICK_MINUTES` that is about one run every two days.
FLX_RUN_ODDS: Final = 576

#: How long a run lasts, in ticks. One to three hours at five minutes a tick.
FLX_RUN_MIN_TICKS: Final = 12
FLX_RUN_MAX_TICKS: Final = 36

#: Per-tick drift during a run, as a percent, signed by its direction. Over a
#: typical run this compounds to roughly a 40% move.
FLX_RUN_DRIFT_PERCENT: Final = 1.5

#: Mean reversion is suppressed but not switched off during a run. At the calm
#: 5% the pull cancels the drift within a few ticks and the price never goes
#: anywhere; at zero a run would ride the bound for its whole length. Leaving a
#: little in makes a run decelerate under its own weight, so its size falls out
#: of the drift and the length instead of needing a separate cap.
FLX_RUN_REVERSION_PERCENT: Final = 1

#: Shock size during a run. Wider than the calm market, so a run reads as
#: volatile rather than as a smooth ramp.
FLX_RUN_VOLATILITY_PERCENT: Final = 5

#: The most Flyxcoin one member may buy in a day.
#:
#: The market is the only place in the game where money multiplies. The walk
#: mean-reverts and :func:`flx_cost` quotes one price to buyer and seller alike,
#: so buying below the anchor and selling above it is a round trip that is
#: profitable every time, and its profit is a percentage of whatever bank the
#: member brought -- the exact shape :data:`DAILY_PAYOUT_CAP` exists to stop.
#: Capping the coins bought per day bounds one day's gain in absolute dollars,
#: which turns a season of trading from exponential into linear.
#:
#: Denominated in coins rather than dollars on purpose: the cap then tightens on
#: its own as the price rises, and it cannot be dodged by waiting for a dip.
#: Buying is capped and selling is not, because a member can only sell what they
#: already bought or mined, and mining is bounded by its own cooldown.
FLX_DAILY_BUY_CAP: Final = 100

#: Minutes between price ticks, driven by the background task in
#: ``cogs/market.py``.
FLX_TICK_MINUTES: Final = 5

#: What the market is currently doing. ``calm`` is the resting state; the other
#: two are runs, and differ only in the sign of the drift.
MarketRegime = Literal["calm", "bull", "bear"]


@dataclass(frozen=True, slots=True)
class MarketState:
    """The whole market, as one tick leaves it.

    A run has to outlive the tick that starts it, which a price alone cannot
    express -- so the regime and its remaining length travel with the price and
    are persisted beside it.

    Attributes:
        price: The live Flyxcoin price, in dollars.
        regime: What the market is doing.
        ticks_left: Ticks remaining in the current run, zero while calm.
    """

    price: int
    regime: MarketRegime = "calm"
    ticks_left: int = 0


#: Wallet balance granted to a brand new account.
STARTING_WALLET: Final = 0

#: Bank balance granted to a brand new account.
STARTING_BANK: Final = 1_000

# ----------------------------------------------------------------- mining ---

#: Miner level granted by the owner-only ``adminme`` command.
ADMIN_MINER_LEVEL: Final = 999

#: Flyxcoin mined per successful ``mine`` at :data:`ADMIN_MINER_LEVEL`.
ADMIN_MINE_YIELD: Final = 10

#: Highest level reachable through the ``upgrade`` command.
MAX_MINER_LEVEL: Final = 5

#: Percent chance that ``mine`` yields one Flyxcoin, keyed by miner level.
MINE_CHANCE_PERCENT: Final[dict[int, int]] = {1: 1, 2: 5, 3: 10, 4: 15, 5: 20}

#: Bank cost to advance from the keyed level to the next one.
UPGRADE_COST: Final[dict[int, int]] = {0: 100, 1: 5_000, 2: 20_000, 3: 100_000, 4: 500_000}

# --------------------------------------------------------------- payouts ----

#: Amount credited on a win, as a multiple of the stake. The stake is debited
#: when the bet is placed, so a multiplier of ``2`` returns the stake plus an
#: equal profit. Net profit is therefore ``multiplier - 1`` times the stake.
COINFLIP_RETURN: Final = 2
RPS_RETURN: Final = 3
DICE_RETURN: Final = 6

#: Rock paper scissors returns nothing on a tie. Refunding the tie is what made
#: the game pay +33% and turned it into a money printer that no rate limit could
#: close, because the profit was per play rather than per second. Losing the tie
#: leaves the game at exactly 0%, in line with coinflip, dice, and war.
RPS_TIE_RETURN: Final = 0
ROULETTE_STRAIGHT_RETURN: Final = 35
ROULETTE_COLOR_RETURN: Final = 2

#: Fraction of the bank balance paid out as daily interest.
DAILY_PAYOUT_RATE: Final = 0.10

#: Ceiling on one account's daily interest, in dollars.
#:
#: This is what keeps a season from hyperinflating. A percentage of the bank
#: compounds, and 10% a day is a factor of 1.28e15 over a year, which is more
#: money than the rest of the economy can produce by fifteen orders of
#: magnitude. Capping the payout leaves the rate intact while the bank is small,
#: so early play feels the same, then flattens growth to a straight line. Every
#: other source is already linear, and linear cannot run away inside a season.
#:
#: The cap is per account per day, and it stayed exactly that when the payout
#: became automatic. What the schedule changed is who collects and how often at
#: most: a claim gated by an in-memory cooldown was reset by every restart,
#: while a once-a-day tick cannot pay twice. The bound is therefore tighter
#: than it was, not looser -- but it now applies to every account rather than
#: only the ones that showed up, so total issuance scales with the number of
#: accounts. That is linear in accounts and capped per account, so it stays
#: inside a season; it is the reason a seeded second account is a straight
#: multiplier on the only faucet that compounds.
DAILY_PAYOUT_CAP: Final = 10_000

#: Inclusive bounds on a successful ``beg``.
BEG_MIN: Final = 1
BEG_MAX: Final = 100

#: One-in-N odds of a ``beg`` succeeding.
BEG_SUCCESS_ODDS: Final = 2

# -------------------------------------------------------------- security ----

#: Highest wallet security level reachable through the ``secure`` command.
MAX_SECURITY_LEVEL: Final = 5

#: Percent chance that a robbery succeeds against the keyed security level.
#:
#: Level 0 is 50%, which is exactly what ``rob`` paid before security existed,
#: so an unprotected wallet is no safer than it ever was. The top level is
#: deliberately not zero: a wallet nobody can ever rob removes the reason to
#: bank money at all, and turns a defensive purchase into a permanent immunity
#: that ends the interaction rather than pricing it.
ROB_SUCCESS_PERCENT: Final[dict[int, int]] = {0: 50, 1: 40, 2: 30, 3: 22, 4: 15, 5: 10}

#: Bank cost to advance wallet security from the keyed level to the next.
#:
#: Security only ever destroys money -- it is bought from the bank and pays
#: nothing back -- so it is a sink, and the prices climb steeply enough that
#: maxing it out is a season-long goal rather than an early purchase.
SECURITY_COST: Final[dict[int, int]] = {
    0: 2_500,
    1: 15_000,
    2: 60_000,
    3: 250_000,
    4: 1_000_000,
}

# ----------------------------------------------------------------- reset ----

#: How long a run of self-resets stays "consecutive".
#:
#: Two resets less than this apart are one chain, and each link in it seeds
#: half of the one before. A gap this long breaks the chain, so a member who
#: leaves it alone for a day starts again at the full :data:`STARTING_BANK`.
#: That is what bounds the faucet: a member may reset as often as they like,
#: but every seed after the first is one they made cheaper themselves, and the
#: whole chain totals less than two hours of begging.
RESET_CYCLE_SECONDS: Final = 60 * 60 * 24

#: How many self-resets in one chain are seeded at all. Each one before this
#: halves the previous seed; every one after it hands over an empty account.
RESET_SEED_HALVINGS: Final = 3


def reset_seed(chained_resets: int) -> int:
    """Return the bank balance a self-reset hands the fresh account.

    The first reset of a chain is a genuine restart and seeds
    :data:`STARTING_BANK`; each one after it halves, and past
    :data:`RESET_SEED_HALVINGS` a reset seeds nothing at all.

    That schedule is what makes resetting a last resort rather than a strategy.
    Nothing stops a member from resetting again immediately -- losing
    everything and starting over is allowed to be a bad day, not an error
    message -- but doing it repeatedly pays less each time, so gambling the
    seed away and going again is a losing move rather than a free one.

    Args:
        chained_resets: How many resets the member has already taken in the
            current chain, which is zero once the chain has expired.

    Returns:
        Dollars to seed the new account's bank with, which may be zero.
    """
    if chained_resets >= RESET_SEED_HALVINGS:
        return 0
    return STARTING_BANK >> chained_resets


def chained_resets(recorded: int, last_reset: float | None, now: float) -> int:
    """Return how many resets count as consecutive with one taken now.

    The stored count is only meaningful while the chain is alive: a member
    whose last reset was over :data:`RESET_CYCLE_SECONDS` ago is starting a
    new one, and reads as zero however many they took before.

    Args:
        recorded: The count stored against the member.
        last_reset: Unix timestamp of their last self-reset, or ``None`` if
            they have never reset.
        now: The current unix timestamp.

    Returns:
        The live chain length, which the seed schedule is keyed by.
    """
    if last_reset is None or now - last_reset >= RESET_CYCLE_SECONDS:
        return 0
    return recorded


def reset_cycle_expires_in(last_reset: float | None, now: float) -> float:
    """Return the seconds until the seed goes back to the full stake.

    The chain breaks a whole cycle after the member's last reset, and their
    next one is then seeded :data:`STARTING_BANK` again.

    Args:
        last_reset: Unix timestamp of their last self-reset, or ``None``.
        now: The current unix timestamp.

    Returns:
        Seconds until the full seed is available again, or ``0.0`` when it
        already is. Never negative, so an odd clock reads as "available".
    """
    if last_reset is None:
        return 0.0
    return max(0.0, RESET_CYCLE_SECONDS - (now - last_reset))


# -------------------------------------------------------------- transfers ---

#: Share of a member-to-member cash transfer withheld as tax.
#:
#: Any rate above zero is enough to send large transfers down the Flyxcoin rail
#: instead, because :func:`flx_cost` quotes one price to a buyer and a seller
#: alike, so a round trip through coins costs nothing but the drift between the
#: buy and the sell. What actually splits the two rails is granularity: coins
#: move in whole units, so Flyxcoin cannot carry anything smaller than one
#: coin's price. This rate therefore prices the convenience of the small rail
#: rather than steering the large one, and raising it steers nothing.
TRANSFER_TAX_RATE: Final = 0.05

#: Share of the tax that feeds the lottery pot, with the remainder going to the
#: creator's bank. The two halves are the whole tax, so a transfer is a pure
#: redistribution rather than a sink -- unlike ``secure``, it destroys nothing.
#: That is safe only because a transfer creates nothing either: the pair always
#: ends poorer by the half they cannot recover, which is what
#: ``tests/test_antiabuse.py`` pins down.
TRANSFER_POT_SHARE: Final = 0.5

#: Smallest transfer ``pay`` accepts.
#:
#: The tax rounds up, so a one dollar transfer would be taxed a dollar and
#: deliver nothing. A floor keeps every transfer worth more to the recipient
#: than the tax takes, at any rate the settings allow.
MIN_TRANSFER: Final = 100


@dataclass(frozen=True, slots=True)
class TransferSplit:
    """How one taxed transfer divides up.

    Attributes:
        amount: Debited from the sender.
        tax: Withheld from ``amount``.
        net: Credited to the recipient.
        pot_share: Added to the lottery pot.
        creator_share: Credited to the creator's bank, or destroyed when no
            creator is configured.
    """

    amount: int
    tax: int
    net: int
    pot_share: int
    creator_share: int


# ------------------------------------------------------------- cooldowns ----

#: Begging creates money from nothing, so its cooldown is what bounds the
#: faucet: at 3 seconds it produced about $30,000 an hour, more than a maximum
#: level miner. At 60 it produces about $1,500.
BEG_COOLDOWN_SECONDS: Final = 60
MINE_COOLDOWN_SECONDS: Final = 60 * 60
ROB_COOLDOWN_SECONDS: Final = 60 * 60

#: The daily payout has no cooldown because it is no longer claimed. It is paid
#: to every account on a schedule, and bounded by :data:`DAILY_PAYOUT_CAP` once
#: per calendar day -- see :data:`DAILY_PAYOUT_PERIOD_SECONDS`.
DAILY_PAYOUT_PERIOD_SECONDS: Final = 60 * 60 * 24

# ---------------------------------------------------------------- games -----

#: How many entries the leaderboard commands return.
LEADERBOARD_SIZE: Final = 10

RockPaperScissors = Literal["rock", "paper", "scissors"]
RPS_MOVES: Final[tuple[RockPaperScissors, ...]] = ("rock", "paper", "scissors")

#: Which move each move defeats.
_RPS_BEATS: Final[dict[str, str]] = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

CoinSide = Literal["heads", "tails"]
COIN_SIDES: Final[tuple[CoinSide, ...]] = ("heads", "tails")

DICE_SIDES: Final = 6

#: An American roulette wheel: 0, 00, and 1-36. ``"00"`` is kept as a string
#: because Python parses the literal ``00`` as the integer ``0``, which would
#: silently collapse the two green pockets into one.
ROULETTE_WHEEL: Final[tuple[int | str, ...]] = (0, "00", *range(1, 37))

ROULETTE_RED: Final[frozenset[int]] = frozenset(
    {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
)
ROULETTE_BLACK: Final[frozenset[int]] = frozenset(
    {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}
)


# ----------------------------------------------------------------- slots ----


@dataclass(frozen=True, slots=True)
class SlotSymbol:
    """One symbol on a slot reel.

    Attributes:
        emoji: How the symbol is drawn in Discord.
        name: Plural plain-text name, so a win reads as "Three cherries!".
        triple_return: Stake multiplier returned for three of this symbol.
        pays_on_pair: Whether exactly two of this symbol pays
            :data:`SLOT_PAIR_RETURN`.
    """

    emoji: str
    name: str
    triple_return: int
    pays_on_pair: bool = False


#: The reel. Every symbol is equally likely, and all three reels are identical,
#: so the 216 possible spins are uniform and the return is exactly computable.
#: `tests/test_economy.py` enumerates every one of them and asserts the house
#: edge, so changing a payout here fails the test until the table is retuned.
SLOT_REEL: Final[tuple[SlotSymbol, ...]] = (
    SlotSymbol("\N{CHERRIES}", "cherries", triple_return=9),
    SlotSymbol("\N{LEMON}", "lemons", triple_return=11),
    SlotSymbol("\N{GRAPES}", "grapes", triple_return=15),
    SlotSymbol("\N{BELL}", "bells", triple_return=22),
    SlotSymbol("\N{WHITE MEDIUM STAR}", "stars", triple_return=35, pays_on_pair=True),
    SlotSymbol("\N{GEM STONE}", "gems", triple_return=55, pays_on_pair=True),
)

#: Reels in one spin.
SLOT_REEL_COUNT: Final = 3

#: Stake multiplier returned for exactly two of a symbol whose
#: :attr:`SlotSymbol.pays_on_pair` is set. Two of anything else pays nothing.
SLOT_PAIR_RETURN: Final = 2

# ------------------------------------------------------------------ cards ---

#: Rank values, where 11 through 14 are jack, queen, king, and ace.
CARD_RANKS: Final[tuple[int, ...]] = tuple(range(2, 15))

CARD_SUITS: Final[tuple[str, ...]] = ("♠", "♥", "♦", "♣")

#: Display names for the ranks that are not written as a number.
_RANK_NAMES: Final[dict[int, str]] = {11: "J", 12: "Q", 13: "K", 14: "A"}


@dataclass(frozen=True, slots=True)
class Card:
    """A playing card.

    Attributes:
        rank: 2 through 14, where 14 is an ace.
        suit: One of :data:`CARD_SUITS`.
    """

    rank: int
    suit: str

    def __str__(self) -> str:
        """Render the card the way it is shown in Discord, such as ``A♠``."""
        return f"{_RANK_NAMES.get(self.rank, str(self.rank))}{self.suit}"


#: A standard 52-card deck.
DECK: Final[tuple[Card, ...]] = tuple(
    Card(rank, suit) for rank in CARD_RANKS for suit in CARD_SUITS
)

#: Stake multiplier returned when the player's card beats the dealer's. A tie
#: returns the stake, so war is an exactly fair game like coinflip and dice.
WAR_WIN_RETURN: Final = 2
WAR_TIE_RETURN: Final = 1


def net_worth(wallet: int, bank: int, crypto: int, price: int = FLX_PRICE) -> int:
    """Return a member's total net worth in dollars.

    Args:
        wallet: Undeposited cash.
        bank: Deposited cash.
        crypto: Flyxcoin held.
        price: Dollar value of one Flyxcoin. Defaults to the base
            :data:`FLX_PRICE`; a caller holding a live quote should pass it.

    Returns:
        Wallet plus bank plus the dollar value of the Flyxcoin.
    """
    return wallet + bank + (crypto * price)


def flx_cost(amount: int, price: int = FLX_PRICE) -> int:
    """Return the dollar cost of ``amount`` Flyxcoin at ``price``."""
    return amount * price


def affordable_flx(bank: int, price: int = FLX_PRICE) -> int:
    """Return the most Flyxcoin that ``bank`` dollars can buy at ``price``."""
    return bank // price


def daily_payout(bank: int, cap: int | None = None) -> int:
    """Return one day's interest for a given bank balance.

    This is the whole rule. The scheduled job in ``cogs/economy.py`` decides
    *when* every account is paid and ``Database.pay_daily_interest`` decides
    that it happens once, but neither one recomputes the amount: a bulk
    ``UPDATE ... SET bank = bank + MIN(bank / 10, cap)`` would fork the
    rounding into SQL, where no unit test can reach it.

    Args:
        bank: The member's current bank balance.
        cap: Ceiling on the payout. Defaults to :data:`DAILY_PAYOUT_CAP`.

    Returns:
        A tenth of the bank, never more than the cap.
    """
    ceiling = DAILY_PAYOUT_CAP if cap is None else cap
    return min(int(bank * DAILY_PAYOUT_RATE), ceiling)


def split_transfer(amount: int, rate: float = TRANSFER_TAX_RATE) -> TransferSplit:
    """Divide a transfer into the recipient's share and the tax's two halves.

    The tax rounds up, so no transfer is ever free. The halves round in the
    creator's favour, which means the odd dollar is destroyed rather than
    recycled whenever no creator is configured -- the safe direction for a
    rounding rule to lean.

    Args:
        amount: Dollars the sender is debited. Must be at least
            :data:`MIN_TRANSFER`.
        rate: Share withheld as tax. Must leave the recipient something, which
            the settings enforce by bounding it well below 1.

    Returns:
        The split, whose ``net`` and ``tax`` always add back up to ``amount``.

    Raises:
        ValueError: If ``amount`` is below :data:`MIN_TRANSFER`.
    """
    if amount < MIN_TRANSFER:
        msg = f"transfer must be at least {MIN_TRANSFER}, got {amount}"
        raise ValueError(msg)

    tax = math.ceil(amount * rate)
    pot_share = int(tax * TRANSFER_POT_SHARE)
    return TransferSplit(
        amount=amount,
        tax=tax,
        net=amount - tax,
        pot_share=pot_share,
        creator_share=tax - pot_share,
    )


def upgrade_cost(miner_level: int) -> int | None:
    """Return the bank cost to upgrade from ``miner_level``.

    Args:
        miner_level: The member's current miner level.

    Returns:
        The cost in dollars, or ``None`` when the miner is already at
        :data:`MAX_MINER_LEVEL` or above.
    """
    return UPGRADE_COST.get(miner_level)


def mine_chance_percent(miner_level: int) -> int:
    """Return the percent chance that a miner at ``miner_level`` yields a coin."""
    return MINE_CHANCE_PERCENT.get(miner_level, 0)


#: Fallback random source. Game outcomes, so a seedable PRNG is correct here.
_DEFAULT_RNG: Final = random.Random()  # noqa: S311


def roll_mine(miner_level: int, rng: random.Random | None = None) -> int:
    """Roll a mining attempt.

    Args:
        miner_level: The member's current miner level.
        rng: Random source, injectable for deterministic tests.

    Returns:
        The number of Flyxcoin mined, which is zero on an unsuccessful roll.
    """
    source = rng if rng is not None else _DEFAULT_RNG
    if miner_level >= ADMIN_MINER_LEVEL:
        return ADMIN_MINE_YIELD
    chance = mine_chance_percent(miner_level)
    return 1 if chance and source.randint(1, 100) <= chance else 0


def security_cost(security_level: int) -> int | None:
    """Return the bank cost to raise wallet security from ``security_level``.

    Args:
        security_level: The member's current security level.

    Returns:
        The cost in dollars, or ``None`` when security is already at
        :data:`MAX_SECURITY_LEVEL` or above.
    """
    return SECURITY_COST.get(security_level)


def rob_success_percent(security_level: int) -> int:
    """Return the percent chance a robbery beats ``security_level``.

    Args:
        security_level: The victim's current security level. A level past the
            top of the table is clamped to it, so a hand-edited row cannot make
            a wallet unrobbable.

    Returns:
        The chance the theft succeeds, which is never zero.
    """
    clamped = min(max(security_level, 0), MAX_SECURITY_LEVEL)
    return ROB_SUCCESS_PERCENT[clamped]


def roll_rob(security_level: int, rng: random.Random | None = None) -> bool:
    """Roll a robbery against a wallet defended at ``security_level``.

    Args:
        security_level: The victim's current security level.
        rng: Random source, injectable for deterministic tests.

    Returns:
        Whether the robbery succeeds.
    """
    source = rng if rng is not None else _DEFAULT_RNG
    return source.randint(1, 100) <= rob_success_percent(security_level)


def next_flx_market(state: MarketState, rng: random.Random | None = None) -> MarketState:
    """Advance the market by one tick.

    A calm tick behaves exactly as the market always has: pull
    :data:`FLX_MEAN_REVERSION_PERCENT` of the gap back toward :data:`FLX_PRICE`,
    apply a shock of up to :data:`FLX_VOLATILITY_PERCENT`, and clamp to
    ``[FLX_PRICE_FLOOR, FLX_PRICE_CEILING]``. That keeps the price hovering near
    the anchor, which is where it sits about seven ticks in eight.

    One calm tick in :data:`FLX_RUN_ODDS` instead starts a run, which lasts
    between :data:`FLX_RUN_MIN_TICKS` and :data:`FLX_RUN_MAX_TICKS` and carries a
    steady drift in one direction. Raising :data:`FLX_VOLATILITY_PERCENT` alone
    could never produce this: mean reversion erases a one-tick spike within the
    hour, so a *run* needs state that survives the tick, which is why this takes
    and returns a whole :class:`MarketState` rather than a price.

    A run is not separately capped. Reversion is suppressed during one rather
    than switched off, so the drift meets a pull that grows with the distance
    travelled and the run decelerates on its own; the bounds are the backstop,
    not the mechanism. When the run expires the calm reversion drags the price
    home over the next hour or two, which is what makes the move read as a run
    and a recovery instead of a permanent step.

    Args:
        state: The market before this tick.
        rng: Random source, injectable for deterministic tests.

    Returns:
        The market after this tick. The price is always at least $1.
    """
    source = rng if rng is not None else _DEFAULT_RNG

    regime: MarketRegime = state.regime
    ticks_left = state.ticks_left
    if regime == "calm" and source.randint(1, FLX_RUN_ODDS) == 1:
        regime = "bull" if source.random() < 0.5 else "bear"
        ticks_left = source.randint(FLX_RUN_MIN_TICKS, FLX_RUN_MAX_TICKS)

    if regime == "calm":
        reversion = FLX_MEAN_REVERSION_PERCENT
        volatility = FLX_VOLATILITY_PERCENT
        drift = 0.0
    else:
        reversion = FLX_RUN_REVERSION_PERCENT
        volatility = FLX_RUN_VOLATILITY_PERCENT
        drift = FLX_RUN_DRIFT_PERCENT if regime == "bull" else -FLX_RUN_DRIFT_PERCENT

    reverted = state.price + (FLX_PRICE - state.price) * reversion / 100
    drifted = reverted * (1 + drift / 100)
    shock = source.uniform(-volatility, volatility) / 100
    moved = drifted * (1 + shock)
    clamped = min(FLX_PRICE_CEILING, max(FLX_PRICE_FLOOR, moved))

    if regime != "calm":
        ticks_left -= 1
        if ticks_left <= 0:
            regime, ticks_left = "calm", 0

    return MarketState(price=max(1, round(clamped)), regime=regime, ticks_left=ticks_left)


def flx_buy_allowance(bought_today: int, cap: int = FLX_DAILY_BUY_CAP) -> int:
    """Return how much more Flyxcoin a member may buy today.

    Args:
        bought_today: Coins the member has already bought today.
        cap: The daily ceiling. Defaults to :data:`FLX_DAILY_BUY_CAP`.

    Returns:
        Coins still available to buy, never negative.
    """
    return max(0, cap - bought_today)


def rps_outcome(player: str, bot_move: str) -> Literal["win", "lose", "tie"]:
    """Return the result of a rock-paper-scissors round from the player's view."""
    if player == bot_move:
        return "tie"
    return "win" if _RPS_BEATS[player] == bot_move else "lose"


def roulette_color(pocket: int | str) -> Literal["red", "black", "green"]:
    """Return the color of a roulette pocket."""
    if pocket in ROULETTE_RED:
        return "red"
    if pocket in ROULETTE_BLACK:
        return "black"
    return "green"


def parse_roulette_bet(raw: str) -> int | str | None:
    """Parse a raw roulette bet into a wheel pocket or a color.

    Args:
        raw: User input, such as ``"red"``, ``"00"``, or ``"17"``.

    Returns:
        ``"red"``/``"black"`` for a color bet, a pocket from
        :data:`ROULETTE_WHEEL` for a straight bet, or ``None`` when the input is
        not a valid bet.
    """
    bet = raw.strip().lower()
    if bet in {"red", "black"}:
        return bet
    if bet == "00":
        return "00"
    try:
        pocket = int(bet)
    except ValueError:
        return None
    return pocket if pocket in ROULETTE_WHEEL else None


def spin_slots(rng: random.Random | None = None) -> tuple[SlotSymbol, ...]:
    """Spin the reels.

    Args:
        rng: Random source, injectable for deterministic tests.

    Returns:
        One symbol per reel, in display order.
    """
    source = rng if rng is not None else _DEFAULT_RNG
    return tuple(source.choice(SLOT_REEL) for _ in range(SLOT_REEL_COUNT))


def slots_payout_multiplier(reels: Sequence[SlotSymbol]) -> int:
    """Return the stake multiplier won by a spin.

    Three of a kind pays that symbol's :attr:`SlotSymbol.triple_return`.
    Otherwise, exactly two of a symbol that pays on pairs returns
    :data:`SLOT_PAIR_RETURN`. Anything else loses.

    Args:
        reels: The spun symbols.

    Returns:
        The multiplier to credit, or ``0`` for a losing spin.
    """
    first = reels[0]
    if all(symbol == first for symbol in reels):
        return first.triple_return

    for symbol in reels:
        # Three reels cannot hold two pairs, so the first match is the only one.
        if symbol.pays_on_pair and reels.count(symbol) == 2:
            return SLOT_PAIR_RETURN
    return 0


def draw_cards(count: int, rng: random.Random | None = None) -> list[Card]:
    """Deal cards off the top of a freshly shuffled deck.

    Cards are dealt without replacement, so two cards can never be identical.

    Args:
        count: How many cards to deal.
        rng: Random source, injectable for deterministic tests.

    Returns:
        The dealt cards.

    Raises:
        ValueError: If more cards are requested than a deck holds.
    """
    if not 0 <= count <= len(DECK):
        msg = f"cannot deal {count} cards from a {len(DECK)}-card deck"
        raise ValueError(msg)
    source = rng if rng is not None else _DEFAULT_RNG
    return source.sample(list(DECK), count)


def war_payout_multiplier(player: Card, dealer: Card) -> int:
    """Return the stake multiplier won by a hand of war.

    Args:
        player: The player's card.
        dealer: The dealer's card.

    Returns:
        :data:`WAR_WIN_RETURN` for a higher card, :data:`WAR_TIE_RETURN` to push
        an equal rank, or ``0`` for a lower card. Suits never break a tie.
    """
    if player.rank > dealer.rank:
        return WAR_WIN_RETURN
    if player.rank == dealer.rank:
        return WAR_TIE_RETURN
    return 0


def roulette_payout_multiplier(bet: int | str, pocket: int | str) -> int:
    """Return the stake multiplier won by ``bet`` against a spun ``pocket``.

    Args:
        bet: A parsed bet from :func:`parse_roulette_bet`.
        pocket: The pocket the ball landed in.

    Returns:
        The multiplier to credit, or ``0`` for a losing bet.
    """
    if bet in {"red", "black"}:
        return ROULETTE_COLOR_RETURN if roulette_color(pocket) == bet else 0
    return ROULETTE_STRAIGHT_RETURN if bet == pocket else 0
