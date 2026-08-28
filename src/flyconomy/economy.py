"""Pure economy rules.

This module holds every tunable number in the game and the pure functions that
act on them. It imports nothing from ``discord`` so the rules can be unit tested
without a gateway connection, and so that rebalancing the economy never requires
touching command-handling code.
"""

from __future__ import annotations

import random
from typing import Final, Literal

# --------------------------------------------------------------- currency ---

#: Price of one Flyxcoin, in dollars. Used for both buying and selling, and as
#: the multiplier that converts a Flyxcoin holding into net worth.
FLX_PRICE: Final = 10_000

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
ROULETTE_STRAIGHT_RETURN: Final = 35
ROULETTE_COLOR_RETURN: Final = 2

#: Fraction of the bank balance paid out by the ``daily`` command.
DAILY_PAYOUT_RATE: Final = 0.10

#: Inclusive bounds on a successful ``beg``.
BEG_MIN: Final = 1
BEG_MAX: Final = 100

#: One-in-N odds of ``beg`` and ``rob`` succeeding.
BEG_SUCCESS_ODDS: Final = 2
ROB_SUCCESS_ODDS: Final = 2

# ------------------------------------------------------------- cooldowns ----

BEG_COOLDOWN_SECONDS: Final = 3
MINE_COOLDOWN_SECONDS: Final = 60 * 60
ROB_COOLDOWN_SECONDS: Final = 60 * 60
DAILY_COOLDOWN_SECONDS: Final = 60 * 60 * 24

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


def net_worth(wallet: int, bank: int, crypto: int) -> int:
    """Return a member's total net worth in dollars.

    Args:
        wallet: Undeposited cash.
        bank: Deposited cash.
        crypto: Flyxcoin held.

    Returns:
        Wallet plus bank plus the dollar value of the Flyxcoin.
    """
    return wallet + bank + (crypto * FLX_PRICE)


def flx_cost(amount: int) -> int:
    """Return the dollar cost of ``amount`` Flyxcoin."""
    return amount * FLX_PRICE


def affordable_flx(bank: int) -> int:
    """Return the most Flyxcoin that ``bank`` dollars can buy."""
    return bank // FLX_PRICE


def daily_payout(bank: int) -> int:
    """Return the ``daily`` command's payout for a given bank balance."""
    return int(bank * DAILY_PAYOUT_RATE)


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
