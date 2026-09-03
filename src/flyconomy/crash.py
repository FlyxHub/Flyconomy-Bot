"""Crash rules.

Like :mod:`flyconomy.blackjack`, this module imports nothing from ``discord``,
so the whole ruleset is unit tested without a gateway connection. A round
draws a hidden crash point when it is dealt; the multiplier a player sees is a
pure function of elapsed time, never of a clock read from inside this module.
That split is what lets the view settle a round from a scheduled tick, a
button press, or a timeout interchangeably, and lets every rule here be tested
with a plain float instead of a real delay.

The multiplier starts at 1.00x and grows at :data:`GROWTH_PER_SECOND` every
second. A round busts at its (hidden, pre-drawn) :attr:`Game.crash_point`;
cashing out before then pays the multiplier at that moment, cashing out after
pays nothing.

House edge is a single constant, :data:`HOUSE_EDGE`, and unlike blackjack it
holds for every cash-out strategy, not just one: targeting any fixed
multiplier ``m > 1`` has an expected profit of exactly ``-HOUSE_EDGE * stake``,
independent of ``m``. See :meth:`Game.deal` for the derivation.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final, Self

#: Share of every wager the house keeps, on average, regardless of the target
#: multiplier a player cashes out at. See :meth:`Game.deal` for why this holds
#: for any strategy, not just one.
HOUSE_EDGE: Final = 0.03

#: The multiplier grows by this factor every second, compounding continuously
#: rather than in the discrete steps the display ticks at.
GROWTH_PER_SECOND: Final = 1.06

#: Hard cap on a round's crash point, so no round can run indefinitely. This
#: can only ever push the realized house edge above :data:`HOUSE_EDGE`, never
#: below it: see :meth:`Game.deal`.
MAX_MULTIPLIER: Final = 20.0

#: View-level safety-net timeout: comfortably past the crash time of even a
#: round that rolled the maximum multiplier, so it only fires if the
#: scheduled tick task has somehow stopped running.
DECISION_TIMEOUT_SECONDS: Final = 75

#: Cosmetic redraw cadence for the live embed. The round's outcome is always
#: computed from elapsed time, never from the last tick, so a slower or
#: skipped tick under load never changes what a cash-out actually pays.
TICK_SECONDS: Final = 2.0

#: Fallback random source. Game outcomes, so a seedable PRNG is correct here.
_DEFAULT_RNG: Final = random.Random()  # noqa: S311


@dataclass(slots=True)
class Game:
    """One crash round.

    Attributes:
        stake: Dollars staked, already debited by the caller.
        crash_point: The multiplier the round busts at, drawn once at deal
            time and never revealed until the round ends.
    """

    stake: int
    crash_point: float

    @classmethod
    def deal(cls, stake: int, rng: random.Random | None = None) -> Self:
        """Draw a hidden crash point for a new round.

        The crash point is sampled so that ``P(crash_point >= m) =
        (1 - HOUSE_EDGE) / m`` for every ``m >= 1``. That makes the expected
        payout of a strategy that cashes out at any fixed target ``m > 1``
        exactly ``P(crash_point >= m) * m * stake = (1 - HOUSE_EDGE) *
        stake`` -- a flat house edge for every target, not just one.

        The draw is a single uniform ``u`` on ``[0, 1)``: with probability
        ``HOUSE_EDGE`` the round busts instantly at 1.00x (the house edge's
        mass folded into round-one busts, the same way a natural blackjack
        short-circuits a hand at deal time); otherwise ``u`` is rescaled back
        onto ``[0, 1)`` and inverted to land on the zero-edge Pareto tail
        ``1 / (1 - u)``, then clamped to :data:`MAX_MULTIPLIER`. Clamping only
        ever removes probability mass from the tail, which can only raise the
        realized edge above :data:`HOUSE_EDGE`, never push it negative.

        Args:
            stake: Dollars staked, already debited by the caller.
            rng: Random source, injectable for deterministic tests.

        Returns:
            The dealt round, with its crash point already decided but not
            yet reached.
        """
        source = rng if rng is not None else _DEFAULT_RNG
        draw = source.random()
        if draw < HOUSE_EDGE:
            crash_point = 1.0
        else:
            rescaled = (draw - HOUSE_EDGE) / (1 - HOUSE_EDGE)
            crash_point = min(1.0 / (1.0 - rescaled), MAX_MULTIPLIER)
        return cls(stake=stake, crash_point=crash_point)


def multiplier_at(elapsed_seconds: float) -> float:
    """Return the multiplier a round would show after ``elapsed_seconds``.

    Ignores whether the round has actually busted by then; callers that care
    should check :func:`has_crashed` or use :func:`current_multiplier`.

    Args:
        elapsed_seconds: Seconds since the round started.

    Returns:
        ``GROWTH_PER_SECOND ** elapsed_seconds``.
    """
    return float(GROWTH_PER_SECOND**elapsed_seconds)


def crash_time_seconds(game: Game) -> float:
    """Return the elapsed time, in seconds, at which ``game`` busts."""
    return math.log(game.crash_point) / math.log(GROWTH_PER_SECOND)


def has_crashed(game: Game, elapsed_seconds: float) -> bool:
    """Return whether ``game`` has busted by ``elapsed_seconds``."""
    return elapsed_seconds >= crash_time_seconds(game)


def current_multiplier(game: Game, elapsed_seconds: float) -> float:
    """Return what a live embed should show for ``game`` at ``elapsed_seconds``.

    Clamped to the crash point, so a round that already busted keeps showing
    the multiplier it busted at instead of one that kept climbing past it.
    """
    return min(multiplier_at(elapsed_seconds), game.crash_point)


def payout(stake: int, multiplier: float) -> int:
    """Return the dollars credited back for cashing out at ``multiplier``.

    Args:
        stake: Everything staked on the round.
        multiplier: The multiplier cashed out at, or ``0.0`` for a bust.

    Returns:
        Dollars to credit. Integer division rounds in the house's favor, the
        same as :func:`flyconomy.blackjack.payout`.
    """
    return int(stake * multiplier)
