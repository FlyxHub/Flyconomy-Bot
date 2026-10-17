"""Server events: an hour when one part of the economy runs hot.

Like the other rules modules, this imports nothing from ``discord``.

Every so often the bot announces an event in the shared announcement channel.
Three of them last :data:`EVENT_MINUTES` and change one rule while they run;
the fourth, a supply drop, is a single button that pays whoever presses it
first.

**An event may never touch a game's payout.** No game may have a positive
expected value, and a "lucky hour" at the casino would be exactly that for an
hour. Events only boost the linear faucets (begging, mining, a drop), which a
fixed season survives at any rate for an hour at a time, or tilt robbery,
which moves money between members and creates none. ``tests/test_events.py``
pins every bound below.

**When nothing is happening, they behave exactly as the plain rules do,** down
to the random numbers they draw, so a quiet server is the same economy it was
before events existed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final

from flyconomy import economy


@dataclass(frozen=True, slots=True)
class Event:
    """One kind of server event.

    Attributes:
        key: Its id, which the owner's ``$event`` takes.
        emoji: Shown beside its name.
        name: Its title.
        blurb: What changes while it runs, as members read it.
    """

    key: str
    emoji: str
    name: str
    blurb: str


#: How long a timed event lasts.
EVENT_MINUTES: Final = 60

#: How often the scheduler considers starting one, and the odds it does when
#: nothing is running: one check in nine, so about three quiet hours apart.
CHECK_MINUTES: Final = 20
START_ODDS: Final = 9

#: Gold Rush multiplies whatever a mine yields.
GOLD_RUSH_MULTIPLIER: Final = 3

#: Charity Drive never comes up empty, and multiplies what a beg pays.
CHARITY_MULTIPLIER: Final = 2

#: Crime Wave adds this many percentage points to every robbery's odds. The
#: best-defended wallet still only falls 30% of the time.
CRIME_WAVE_BONUS_PERCENT: Final = 20

#: A supply drop pays a random amount in this range into the grabber's
#: wallet, where it can be robbed like any other cash.
DROP_MIN: Final = 2_500
DROP_MAX: Final = 10_000

#: How long a supply drop waits to be grabbed before it is gone.
DROP_TIMEOUT_SECONDS: Final = 15 * 60

GOLD_RUSH: Final = Event(
    "goldrush",
    "⛏️",
    "Gold Rush",
    f"Every `/mine` yields {GOLD_RUSH_MULTIPLIER}x the Flyxcoin.",
)
CHARITY_DRIVE: Final = Event(
    "charity",
    "🙏",
    "Charity Drive",
    f"`/beg` never comes up empty, and pays {CHARITY_MULTIPLIER}x.",
)
CRIME_WAVE: Final = Event(
    "crimewave",
    "🚨",
    "Crime Wave",
    f"Every `/rob` is {CRIME_WAVE_BONUS_PERCENT} points more likely to land. "
    "Get your cash into the bank.",
)
SUPPLY_DROP: Final = Event(
    "drop",
    "📦",
    "Supply Drop",
    "A briefcase of cash just landed. First to grab it keeps it.",
)

#: The events that last a while, rather than happening at once.
TIMED: Final[tuple[Event, ...]] = (GOLD_RUSH, CHARITY_DRIVE, CRIME_WAVE)

ALL: Final[tuple[Event, ...]] = (*TIMED, SUPPLY_DROP)

BY_KEY: Final[dict[str, Event]] = {e.key: e for e in ALL}


def pick(rng: random.Random) -> Event:
    """Choose which event happens next, each equally likely."""
    return rng.choice(ALL)


def mine_yield(mined: int, active: Event | None) -> int:
    """Return what a mine that rolled ``mined`` coins actually yields."""
    return mined * GOLD_RUSH_MULTIPLIER if active is GOLD_RUSH else mined


def roll_beg(rng: random.Random, active: Event | None) -> int:
    """Roll a beg: the dollars it pays, or zero for nothing.

    Draws the same numbers as the plain rule outside a Charity Drive.
    """
    if active is not CHARITY_DRIVE and rng.randint(1, economy.BEG_SUCCESS_ODDS) == 1:
        return 0
    amount = rng.randint(economy.BEG_MIN, economy.BEG_MAX)
    return amount * CHARITY_MULTIPLIER if active is CHARITY_DRIVE else amount


def rob_success_percent(security_level: int, active: Event | None) -> int:
    """Return the percent chance a robbery beats ``security_level`` right now."""
    bonus = CRIME_WAVE_BONUS_PERCENT if active is CRIME_WAVE else 0
    return economy.rob_success_percent(security_level) + bonus


def roll_rob(security_level: int, active: Event | None, rng: random.Random) -> bool:
    """Roll a robbery, drawing the same number :func:`economy.roll_rob` does."""
    return rng.randint(1, 100) <= rob_success_percent(security_level, active)


def drop_amount(rng: random.Random) -> int:
    """Roll what a supply drop holds."""
    return rng.randint(DROP_MIN, DROP_MAX)
