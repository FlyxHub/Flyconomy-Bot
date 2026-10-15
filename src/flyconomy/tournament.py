"""Weekly tournament rules.

Like :mod:`flyconomy.jackpot`, this module imports nothing from ``discord``, so
the whole ruleset is unit tested without a gateway connection.

A tournament runs for one ISO week, Monday to Sunday in the bot's timezone.
Members buy in for :data:`ENTRY_FEE`, and whoever finishes the week with the
best **net result at the house's games** -- what they got back less what they
staked, from the moment they joined -- takes the pot less :data:`HOUSE_CUT`.

**It is player-funded, so it can only shrink the supply.** The pot is nothing
but the entry fees, and the winner is paid less than that. Nothing is minted,
and the games that set the score keep their own edges, so playing for the
tournament costs exactly what playing without it would.

**Only games against the house count.** Tic-tac-toe and the jackpot are
decided by other members, so a pair of alts could hand one of them a score by
losing to it on purpose. Against the house, a score can only be bought with
money actually put at risk.

**Doing nothing cannot win.** Every house game has an expected value at or
below zero, so a member who joins and never plays would sit on a score of zero
while the field drifts below it. :data:`MIN_WAGERED` is the qualifier that
rules that out: a member who has not staked at least the entry fee over the
week is in the pot but cannot take it.
"""

from __future__ import annotations

import datetime
from typing import Final

#: What one entry costs, paid from the bank into the pot.
ENTRY_FEE: Final = 10_000

#: Share of the pot the house keeps when it is won, matching the other
#: player-funded pots. The winner is always paid less than the fees came to.
HOUSE_CUT: Final = 0.05

#: Total staked at the house's games over the week before an entrant can win.
MIN_WAGERED: Final = ENTRY_FEE

#: Qualified entrants needed for the pot to be won. Fewer, and every fee goes
#: back: with one there is nobody to beat, so taking a cut would be charging
#: for a contest that never happened.
MIN_ENTRANTS: Final = 2

#: Entrants named on the standings, in Discord and on the dashboard.
STANDINGS_SHOWN: Final = 10


def week_of(when: datetime.datetime) -> str:
    """Return the tournament week a moment falls in, as ``YYYY-Www``.

    ISO weeks start on Monday, and the string sorts in time order because the
    week is zero-padded, which the database relies on.

    Args:
        when: A timezone-aware moment, in the bot's timezone.

    Returns:
        The ISO year and week, such as ``2026-W42``.
    """
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def week_ends(when: datetime.datetime) -> datetime.datetime:
    """Return when the week containing ``when`` ends: the next Monday at midnight.

    Args:
        when: A timezone-aware moment, in the bot's timezone.

    Returns:
        Midnight starting the following Monday, in the same timezone.
    """
    monday = when.date() + datetime.timedelta(days=7 - when.weekday())
    return datetime.datetime.combine(monday, datetime.time(), tzinfo=when.tzinfo)


def qualifies(wagered: int) -> bool:
    """Return whether an entrant has staked enough to be able to win."""
    return wagered >= MIN_WAGERED


def house_cut(pot: int) -> int:
    """Return the dollars the house keeps from a won pot, truncated down."""
    return int(pot * HOUSE_CUT)


def payout(pot: int) -> int:
    """Return what the winner takes: the pot less :func:`house_cut`."""
    return pot - house_cut(pot)
