"""Jackpot rules.

Like :mod:`flyconomy.crash`, this module imports nothing from ``discord``, so
the whole ruleset is unit tested without a gateway connection.

A jackpot round is player-funded: every entrant antes their own money into one
pot, the round closes after :data:`ROUND_SECONDS`, and a single entrant takes
the whole pot less :data:`HOUSE_CUT`. No money is created — the payout is
always smaller than the sum of the antes, so a round can only ever shrink the
supply.

**Odds are proportional to the ante, and that is what makes the game fair to
everyone at once.** An entrant holding ``a`` of a pot of ``p`` wins with
probability ``a / p`` and takes ``p * (1 - HOUSE_CUT)``, so their expected
return is ``a * (1 - HOUSE_CUT)``: an edge of exactly :data:`HOUSE_CUT`,
whatever they anted and however many others entered. Nobody can buy better
odds than anybody else, because the price of every extra unit of chance is the
unit of money that funds it.

That is a deliberate departure from the lottery next door, where odds cannot
be bought at any price. The two rules differ because the two pots are funded
differently: the lottery's pot is fed by the house's rake, which is money no
entrant paid in, so a bought entry would be a bought claim on other members'
losses. A jackpot pot is nothing but the antes of the people playing for it.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Share of the pot the house keeps from a decided round. This is the whole of
#: the game's edge: every entrant's expected return is ``1 - HOUSE_CUT`` times
#: their ante, no matter how large a share of the pot they hold.
HOUSE_CUT: Final = 0.05

#: How long a round accepts antes before it draws.
ROUND_SECONDS: Final = 60.0

#: Cosmetic redraw cadence for the live embed. The round closes on elapsed
#: time, never on the last tick, so a slow or skipped redraw cannot extend or
#: shorten the window a member has to join.
TICK_SECONDS: Final = 5.0

#: View-level safety-net timeout, comfortably past the end of a round, so it
#: only fires if the scheduled tick task has somehow stopped running.
DECISION_TIMEOUT_SECONDS: Final = ROUND_SECONDS + 30.0

#: Entrants needed to actually draw. A round that closes below this is refunded
#: in full: with one entrant there is nobody to win the pot from, so taking a
#: cut of it would be charging for a game that never happened.
MIN_ENTRANTS: Final = 2


@dataclass(frozen=True, slots=True)
class Entry:
    """One member's ante in a round.

    Attributes:
        user_id: The member's Discord snowflake.
        amount: Dollars anted, already debited from their wallet.
    """

    user_id: int
    amount: int


def total_pot(entries: Sequence[Entry]) -> int:
    """Return the pot the given entries add up to."""
    return sum(entry.amount for entry in entries)


def house_cut(pot: int) -> int:
    """Return the dollars the house keeps from a decided pot.

    Args:
        pot: Everything anted into the round.

    Returns:
        The house's cut. Integer truncation rounds this down, which favours
        the players by a dollar rather than the house.
    """
    return int(pot * HOUSE_CUT)


def payout(pot: int) -> int:
    """Return the dollars the winner takes from a decided pot.

    Args:
        pot: Everything anted into the round.

    Returns:
        The pot less :func:`house_cut`, which is always less than the pot, so
        a round cannot pay out more than was anted into it.
    """
    return pot - house_cut(pot)


def win_chance(amount: int, pot: int) -> float:
    """Return an entrant's chance of winning, as a fraction of one.

    Args:
        amount: The entrant's ante.
        pot: Everything anted into the round.

    Returns:
        ``amount / pot``, or ``0.0`` for an empty pot.
    """
    if pot <= 0:
        return 0.0
    return amount / pot


def draw_winner(entries: Sequence[Entry], rng: random.Random) -> int:
    """Draw one entrant, weighted by ante.

    The ticket is drawn as an integer in ``[0, pot)`` and walked against a
    running total of the antes, so each entrant's chance is exactly their share
    of the pot -- no float rounding stands between an ante and the odds it
    bought.

    Args:
        entries: The round's entries. Must not be empty, and must total more
            than zero.
        rng: Random source, injectable for deterministic tests.

    Returns:
        The winning member's Discord snowflake.

    Raises:
        ValueError: If there is nothing to draw from.
    """
    pot = total_pot(entries)
    if pot <= 0:
        msg = "cannot draw a winner from an empty pot"
        raise ValueError(msg)

    ticket = rng.randrange(pot)
    running = 0
    for entry in entries:
        running += entry.amount
        if ticket < running:
            return entry.user_id
    # Unreachable: the ticket is below the pot, which is the final running
    # total, so some entry always claims it.
    raise AssertionError(pot)  # pragma: no cover
