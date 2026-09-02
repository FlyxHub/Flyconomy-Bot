"""Logging setup."""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging for the process.

    Logs go to stdout so that a container runtime or ``journald`` can collect
    them; the bot never writes its own log files.

    Args:
        level: Root log level name, such as ``"INFO"``.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # discord.py logs every gateway heartbeat at DEBUG, which drowns out our own
    # messages when the root level is lowered for troubleshooting.
    logging.getLogger("discord").setLevel(max(logging.INFO, root.level))
    # discord.py's HTTP client logs its proactive rate-limit waits at DEBUG too --
    # cap that at WARNING in normal operation, but let a DEBUG root level through
    # so a rate-limit stall is visible instead of silently eating command time.
    logging.getLogger("discord.http").setLevel(logging.WARNING if root.level > logging.DEBUG else logging.DEBUG)
