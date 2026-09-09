"""A live, volatile Flyxcoin price, driven by a scheduled random walk.

The price ticks on a timer rather than in response to trades, so buying or
selling can never move it. A trade-driven price would turn the market into a
new game, which would need its own proof of a non-positive expected value the
way the casino games do. A tick also updates the bot's status, so the price
reads like a stock ticker without a member having to ask for it.

Most ticks are quiet. Occasionally one starts a bull or bear run, which is the
only thing here that persists across ticks -- see :func:`economy.next_flx_market`
for why a run needs state rather than just a wider shock.
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
        """Advance the market by one tick and update the bot's status."""
        try:
            previous = await self.db.get_market()
            state = economy.next_flx_market(previous, self.rng)
            await self.db.set_market(state)
            log.info(
                "Flyxcoin price moved from %d to %d (%s)",
                previous.price,
                state.price,
                state.regime,
            )
            if previous.regime == "calm" and state.regime != "calm":
                await self._tip_the_creator(state)
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"FLX: {embeds.flx_ticker(state.price, previous.price)}",
                )
            )
        except Exception:
            # A failed tick must not kill the loop, or the price freezes.
            log.exception("Flyxcoin price tick failed; will try again next interval")

    async def _tip_the_creator(self, state: economy.MarketState) -> None:
        """DM the creator that a run has just started, best effort.

        A perk rather than a mechanic: the run is already stored by the time
        this is called, so a creator who is unset, unreachable, or has DMs
        closed changes nothing about the market. It gets its own ``try`` inside
        the tick's so a ``Forbidden`` here cannot cost the status update, which
        is what every other member sees the price through.

        The tip is deliberately worth little in money terms. Knowing a run has
        started only helps to the extent you can act on it, and
        :data:`economy.FLX_DAILY_BUY_CAP` bounds that to the same coins a
        member without the DM could have bought anyway.
        """
        creator_id = self.settings.creator_tax_user_id
        if creator_id is None:
            return

        creator = await self.resolve_user(creator_id)
        if creator is None:
            return

        try:
            await creator.send(embed=embeds.market_run_embed(state, self.timezone))
        except discord.HTTPException:
            log.warning("Could not DM the creator (%d) about the %s run", creator_id, state.regime)

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
