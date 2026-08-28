"""Shared base class for the command cogs."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    from flyconomy.bot import FlyconomyBot
    from flyconomy.config import Settings
    from flyconomy.database import Database


class BaseCog(commands.Cog):
    """A cog with typed access to the bot's database and settings.

    Attributes:
        bot: The running client.
        rng: Random source for game outcomes. Tests replace this with a seeded
            instance to make outcomes deterministic.
    """

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog to a running bot."""
        self.bot = bot
        self.rng = random.Random()  # noqa: S311 - game outcomes, not cryptography

    @property
    def db(self) -> Database:
        """The open database."""
        return self.bot.db

    @property
    def settings(self) -> Settings:
        """The bot's runtime configuration."""
        return self.bot.settings

    @property
    def timezone(self) -> str:
        """IANA timezone used for embed timestamps."""
        return self.bot.settings.timezone
