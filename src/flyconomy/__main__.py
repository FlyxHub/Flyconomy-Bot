"""Process entry point.

Run with ``python -m flyconomy`` or the installed ``flyconomy-bot`` command.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from pydantic import ValidationError

from flyconomy import __version__
from flyconomy.bot import FlyconomyBot
from flyconomy.config import Settings, load_settings
from flyconomy.logging_config import configure_logging

log = logging.getLogger("flyconomy")

_EXIT_OK = 0
_EXIT_CONFIG_ERROR = 2
_EXIT_AUTH_ERROR = 3


async def run(settings: Settings) -> None:
    """Start the bot and run until the process is interrupted.

    Args:
        settings: Validated runtime configuration.
    """
    bot = FlyconomyBot(settings)
    # discord.py installs its own logging handlers unless told not to; ours are
    # already configured, so suppress the duplicate setup.
    async with bot:
        await bot.start(settings.discord_token.get_secret_value())


def main() -> int:
    """Load configuration, start the bot, and translate failures into exit codes.

    Returns:
        A process exit code: ``0`` on a clean shutdown, ``2`` for a
        configuration problem, and ``3`` for a rejected token.
    """
    try:
        settings = load_settings()
    except ValidationError as exc:
        configure_logging("INFO")
        log.error("Configuration is invalid. Copy .env.example to .env and fill it in.\n%s", exc)
        return _EXIT_CONFIG_ERROR

    configure_logging(settings.log_level)
    log.info("Starting Flyconomy %s on Python %s", __version__, sys.version.split()[0])

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down")
    except discord.LoginFailure:
        log.error("Discord rejected the bot token. Check FLYCONOMY_DISCORD_TOKEN.")
        return _EXIT_AUTH_ERROR
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord refused the privileged intents. Enable the Message Content and "
            "Server Members intents for this application in the Discord developer portal."
        )
        return _EXIT_CONFIG_ERROR
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
