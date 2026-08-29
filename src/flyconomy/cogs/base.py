"""Shared base class for the command cogs."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from discord.ext import commands

from flyconomy.errors import RateLimitedError

if TYPE_CHECKING:
    from flyconomy.bot import FlyconomyBot
    from flyconomy.config import Settings
    from flyconomy.database import Database
    from flyconomy.ratelimit import SlidingWindowLimiter


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

    @property
    def limiter(self) -> SlidingWindowLimiter:
        """The shared per-member action budget."""
        return self.bot.limiter

    async def rake(self, house_take: int) -> None:
        """Send the configured share of the house's take to the lottery pot.

        Args:
            house_take: What the house won on a wager, negative when the player
                won. Passing the signed figure rather than the gross loss is
                what stops a fair game from being churned to inflate the pot:
                over time a fair game nets the house nothing, so it contributes
                nothing.
        """
        share = int(house_take * self.settings.lottery_rake)
        if share:
            await self.db.add_to_pot(share)

    async def cog_check(self, ctx: commands.Context[FlyconomyBot]) -> bool:  # type: ignore[override]
        """Spend one action from the member's budget.

        Applied to every command in every cog that inherits this, so a member
        cannot escape it by rotating between commands. It also covers the
        commands that refund their own cooldown when they decline to act, such
        as mining without a miner, which would otherwise loop for free.

        Args:
            ctx: Invocation context.

        Returns:
            ``True`` when the member has budget left.

        Raises:
            RateLimitedError: If the member is acting too quickly.
        """
        wait = self.limiter.acquire(ctx.author.id)
        if wait:
            raise RateLimitedError(wait)
        return True
