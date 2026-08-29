"""Per-user rate limiting.

A sliding window rather than discord.py's per-command cooldowns, because the
limit that matters is on a member's total activity: a per-command cooldown can
be sidestepped by rotating between commands, and it cannot cover a command that
refunds its own cooldown when it declines to act.

Like the rules modules, this imports nothing from ``discord`` and takes an
injectable clock, so the behaviour is tested without sleeping.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Final

#: Once more keys than this are tracked, an insert prunes the idle ones. Only
#: bounds memory; it has no effect on whether an action is allowed.
_PRUNE_THRESHOLD: Final = 1_024


class SlidingWindowLimiter:
    """Allows ``rate`` actions per ``per`` seconds for each key.

    Unlike a fixed window, this never lets a member spend a whole window's
    budget at the end of one window and again at the start of the next.
    """

    def __init__(self, rate: int, per: float) -> None:
        """Configure the window.

        Args:
            rate: Actions allowed in each window. Must be at least 1.
            per: Window length in seconds. Must be positive.

        Raises:
            ValueError: If either bound is not positive.
        """
        if rate < 1:
            msg = f"rate must be at least 1, got {rate}"
            raise ValueError(msg)
        if per <= 0:
            msg = f"per must be positive, got {per}"
            raise ValueError(msg)

        self.rate = rate
        self.per = per
        self._hits: dict[int, deque[float]] = {}

    def _clock(self, now: float | None) -> float:
        """Return the caller's timestamp, or read the monotonic clock."""
        return time.monotonic() if now is None else now

    def _trim(self, key: int, now: float) -> deque[float]:
        """Return the recorded hits for a key, dropping any that have expired.

        Does not create an entry for an unknown key, so merely asking about a
        member does not allocate.
        """
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        cutoff = now - self.per
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def retry_after(self, key: int, now: float | None = None) -> float:
        """Return how long until the key may act again.

        Args:
            key: Whoever is being limited, usually a Discord user ID.
            now: Timestamp to judge against. Defaults to the monotonic clock.

        Returns:
            ``0.0`` if the action is allowed right now, otherwise the seconds
            remaining until the oldest recorded action leaves the window.
        """
        moment = self._clock(now)
        hits = self._trim(key, moment)
        if len(hits) < self.rate:
            return 0.0
        return max(0.0, hits[0] + self.per - moment)

    def acquire(self, key: int, now: float | None = None) -> float:
        """Record an action if the key has budget for one.

        Args:
            key: Whoever is being limited.
            now: Timestamp to record. Defaults to the monotonic clock.

        Returns:
            ``0.0`` if the action was recorded, otherwise the seconds to wait.
            A refused action is not recorded, so hammering the limit does not
            extend it.
        """
        moment = self._clock(now)
        wait = self.retry_after(key, moment)
        if wait > 0:
            return wait

        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= _PRUNE_THRESHOLD:
                self.prune(moment)
            hits = deque(maxlen=self.rate)
            self._hits[key] = hits
        hits.append(moment)
        return 0.0

    def reset(self, key: int) -> None:
        """Forget a key's history, letting it act immediately."""
        self._hits.pop(key, None)

    def prune(self, now: float | None = None) -> int:
        """Drop keys whose windows have fully expired.

        Args:
            now: Timestamp to judge against. Defaults to the monotonic clock.

        Returns:
            How many keys were dropped.
        """
        moment = self._clock(now)
        cutoff = moment - self.per
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        return len(stale)

    @property
    def tracked(self) -> int:
        """How many keys currently hold history. Used by tests and diagnostics."""
        return len(self._hits)
