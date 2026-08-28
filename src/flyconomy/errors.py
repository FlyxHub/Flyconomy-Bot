"""Exceptions raised by the economy layer."""

from __future__ import annotations


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
