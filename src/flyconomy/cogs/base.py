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
        """Split the house's take between the lottery pot and the creator tax.

        The remainder, after both shares, is still destroyed — the creator tax
        is carved out of that destroyed portion rather than added on top, so
        the total taken from a loss does not change.

        Args:
            house_take: What the house won on a wager, negative when the player
                won. A player win contributes to neither share — ``add_to_pot``
                and the tax below both ignore non-positive amounts, and neither
                is ever clawed back out.
        """
        share = int(house_take * self.settings.lottery_rake)
        if share > 0:
            await self.db.add_to_pot(share)

        creator_id = self.settings.creator_tax_user_id
        if creator_id is not None:
            cut = int(house_take * self.settings.creator_tax_rate)
            if cut > 0:
                await self.db.add_bank(creator_id, cut)

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
