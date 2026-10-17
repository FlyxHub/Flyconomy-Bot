"""Achievements: badges a member earns by reaching a milestone.

Like the other rules modules, this imports nothing from ``discord``.

**Every achievement is one number reaching one goal.** The numbers are a
member's lifetime career stats (kept in the ``career`` table as they play) and
their account as it stands, so the catalogue below is pure data and
:func:`earned` is the only rule. A new badge is a row here, plus a counter in
the database if it needs one nobody keeps yet.

**They pay nothing, and that is load-bearing.** Several are farmable -- a pair
of alts can play tic-tac-toe to hand each other Duelist -- which is harmless
only because a badge is worth nothing. One that paid would be a faucet whose
rate is set by how easy its milestone is to farm.

**They are kept for good.** A self-reset keeps the badges and the career stats
behind them, the same way it keeps the ``resets`` row; only a purge, the staff
path, clears them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from flyconomy import economy


@dataclass(frozen=True, slots=True)
class Achievement:
    """One badge.

    Attributes:
        key: Stable id, stored against the member. Never rename one.
        emoji: How it shows on ``/balance``.
        name: Its title.
        description: What earns it, as the dashboard shows it.
        stat: The number it watches. See :func:`earned` for where they come from.
        goal: The value ``stat`` must reach.
    """

    key: str
    emoji: str
    name: str
    description: str
    stat: str
    goal: int


#: Every achievement, in the order ``/balance`` and the dashboard show them.
ALL: Final[tuple[Achievement, ...]] = (
    # Against the house.
    Achievement("first_bet", "🎲", "First Bet", "Settle a bet against the house.", "plays", 1),
    Achievement("regular", "🎰", "Regular", "Settle 100 bets against the house.", "plays", 100),
    Achievement(
        "legend", "👑", "Casino Legend", "Settle 1,000 bets against the house.", "plays", 1_000
    ),
    Achievement(
        "high_roller",
        "💵",
        "High Roller",
        "Stake $1,000,000 in total at the house's games.",
        "wagered",
        1_000_000,
    ),
    Achievement(
        "whale",
        "🐋",
        "Whale",
        "Stake $100,000,000 in total at the house's games.",
        "wagered",
        100_000_000,
    ),
    Achievement(
        "big_win", "💥", "Big Win", "Win back 10x your stake on one bet.", "best_multiple", 10
    ),
    Achievement(
        "moonshot", "🚀", "Moonshot", "Win back 50x your stake on one bet.", "best_multiple", 50
    ),
    Achievement(
        "score", "💎", "The Score", "Profit $1,000,000 on one bet.", "best_profit", 1_000_000
    ),
    # Against each other.
    Achievement("pickpocket", "🦹", "Pickpocket", "Rob someone successfully.", "robberies", 1),
    Achievement("kingpin", "🏴‍☠️", "Kingpin", "Rob 25 people successfully.", "robberies", 25),
    Achievement("bounty_hunter", "🎯", "Bounty Hunter", "Collect a bounty.", "bounties", 1),
    Achievement(
        "safecracker",
        "🔓",
        "Safecracker",
        "Rob a wallet at the top security level.",
        "cracked",
        economy.MAX_SECURITY_LEVEL,
    ),
    Achievement("duelist", "⚔️", "Duelist", "Win a tic-tac-toe match.", "match_wins", 1),
    # Pots.
    Achievement("lucky_ticket", "🎟️", "Lucky Ticket", "Win the lottery.", "lottery_wins", 1),
    Achievement("pot_luck", "🎉", "Pot Luck", "Win a jackpot round.", "jackpot_wins", 1),
    Achievement("champion", "🏆", "Champion", "Win the weekly tournament.", "tournament_wins", 1),
    Achievement("finders_keepers", "📦", "Finders Keepers", "Grab a supply drop.", "drops", 1),
    # What you own right now.
    Achievement("prospector", "⛏️", "Prospector", "Own a Flyxcoin miner.", "miner", 1),
    Achievement(
        "magnate",
        "🏭",
        "Mining Magnate",
        "Upgrade your miner to the top level.",
        "miner",
        economy.MAX_MINER_LEVEL,
    ),
    Achievement(
        "fort_knox",
        "🛡️",
        "Fort Knox",
        "Upgrade your wallet security to the top level.",
        "security",
        economy.MAX_SECURITY_LEVEL,
    ),
    Achievement("hodler", "🪙", "Hodler", "Hold 100 Flyxcoin at once.", "crypto", 100),
    Achievement(
        "millionaire",
        "💰",
        "Millionaire",
        "Reach $1,000,000 in net worth.",
        "net_worth",
        1_000_000,
    ),
    Achievement(
        "billionaire",
        "🏦",
        "Billionaire",
        "Reach $1,000,000,000 in net worth.",
        "net_worth",
        1_000_000_000,
    ),
)

BY_KEY: Final[dict[str, Achievement]] = {a.key: a for a in ALL}


def earned(values: Mapping[str, int]) -> list[Achievement]:
    """Return every achievement ``values`` has reached, in catalogue order.

    Args:
        values: A member's career stats (``plays``, ``wagered``,
            ``best_multiple``, ``best_profit``, ``robberies``, ``bounties``,
            ``cracked``, ``match_wins``, ``lottery_wins``, ``jackpot_wins``,
            ``tournament_wins``, ``drops``) beside their account as it stands
            (``miner``, ``security``, ``crypto``, ``net_worth``). A missing
            stat counts as zero.

    Returns:
        The achievements reached, whether or not they were already awarded.
    """
    return [a for a in ALL if values.get(a.stat, 0) >= a.goal]


def best_multiple(staked: int, returned: int) -> int:
    """Return how many whole times over a bet paid back its stake.

    Whole times, rounded down, so a crash cashed out at 9.99x is not a 10x win.
    """
    return returned // staked if staked > 0 else 0
