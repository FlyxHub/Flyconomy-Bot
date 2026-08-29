"""Exceptions raised by the economy layer.

Most of these are plain exceptions with no framework dependency. The one
exception is :class:`RateLimitedError`, which is raised from a cog check:
discord.py only routes subclasses of ``CommandError`` to an error handler, so an
ordinary exception raised there would reach the member as silence and a logged
traceback rather than as a refusal.
"""

from __future__ import annotations

from discord.ext import commands


class FlyconomyError(Exception):
    """Base class for every error this bot raises deliberately."""


class InsufficientFundsError(FlyconomyError):
    """A debit was rejected because it would overdraw an account.

    Attributes:
        available: The balance the member actually holds.
        requested: The amount the member tried to spend.
        currency: Human-readable name of what ran short.
    """

    def __init__(self, available: int, requested: int, currency: str = "funds") -> None:
        """Store the shortfall and build a display message."""
        self.available = available
        self.requested = requested
        self.currency = currency
        super().__init__(f"insufficient {currency}: has {available}, needs {requested}")


class RateLimitedError(FlyconomyError, commands.CheckFailure):
    """An action was refused because the member is acting too quickly.

    Subclasses ``CheckFailure`` so that discord.py routes it to the bot's error
    handler from a cog check, on both the slash and the prefix path.

    Attributes:
        retry_after: Seconds until the member may act again.
    """

    def __init__(self, retry_after: float) -> None:
        """Store the wait and build a display message."""
        self.retry_after = retry_after
        super().__init__(f"rate limited for another {retry_after:.1f}s")


class BetTooLargeError(FlyconomyError):
    """A wager was refused for exceeding the table limit.

    Attributes:
        bet: What the member tried to stake.
        limit: The most they are allowed to stake on one wager.
    """

    def __init__(self, bet: int, limit: int) -> None:
        """Store the wager and the limit, and build a display message."""
        self.bet = bet
        self.limit = limit
        super().__init__(f"bet of {bet} exceeds the table limit of {limit}")
