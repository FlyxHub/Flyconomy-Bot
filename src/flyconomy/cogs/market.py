"""A live, volatile Flyxcoin price, driven by a scheduled random walk.

The price ticks on a timer rather than in response to trades, so buying or
selling can never move it. A trade-driven price would turn the market into a
new game, which would need its own proof of a non-positive expected value the
way the casino games do. A tick also updates the bot's status, so the price
reads like a stock ticker without a member having to ask for it.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import tasks

from flyconomy import economy, embeds
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog

log = logging.getLogger(__name__)


class Market(BaseCog, name="Market"):
    """Ticks the Flyxcoin price and reflects it in the bot's status."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog and start the price timer."""
        super().__init__(bot)
        self.tick_loop.start()

    async def cog_unload(self) -> None:
        """Stop the price timer when the extension is unloaded."""
        self.tick_loop.cancel()

    @tasks.loop(minutes=economy.FLX_TICK_MINUTES)
    async def tick_loop(self) -> None:
        """Advance the price by one tick and update the bot's status."""
        try:
            previous = await self.db.get_flx_price()
            price = economy.next_flx_price(previous, self.rng)
            await self.db.set_flx_price(price)
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=embeds.flx_ticker(price, previous),
                )
            )
        except Exception:
            # A failed tick must not kill the loop, or the price freezes.
            log.exception("Flyxcoin price tick failed; will try again next interval")

    @tick_loop.before_loop
    async def _before_tick_loop(self) -> None:
        """Wait until the bot is connected before the first tick.

        A client that never logged in has no gateway to wait on and no status
        to set, which is the case in unit tests and when startup failed. There
        is nothing to schedule against then, so the timer stops rather than
        ticking into the void.
        """
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug("No gateway connection; the Flyxcoin price timer will not run")
            self.tick_loop.cancel()


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Market(bot))
