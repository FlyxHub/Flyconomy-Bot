"""Server events: the scheduler that starts them, and the command that shows one.

The rules are in :mod:`flyconomy.events`; the commands an event changes ask
``BaseCog.event`` which one is running, so this cog only decides when.
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands, tasks

from flyconomy import embeds, events
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog
from flyconomy.views import SupplyDropView

log = logging.getLogger(__name__)


class Events(BaseCog, name="Events"):
    """Server-wide events that start on their own."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog and start the scheduler."""
        super().__init__(bot)
        # ponytail: the running event lives in memory, so a restart ends it
        # early. Nothing is owed when one ends, so losing it costs nothing.
        self.active: tuple[events.Event, float] | None = None
        self.event_loop.start()

    async def cog_unload(self) -> None:
        """Stop the scheduler when the extension is unloaded."""
        self.event_loop.cancel()

    def current(self) -> events.Event | None:
        """The timed event running right now, or ``None``."""
        if self.active is None or self.active[1] <= time.time():
            return None
        return self.active[0]

    async def start(self, event: events.Event, channel: discord.abc.Messageable) -> None:
        """Start ``event`` now and announce it in ``channel``.

        A timed event replaces whichever one was running. It only starts once
        its announcement has landed, so an event nobody was told about cannot
        run silently.
        """
        if event is events.SUPPLY_DROP:
            view = SupplyDropView(
                db=self.db, amount=events.drop_amount(self.rng), timezone=self.timezone
            )
            view.message = await channel.send(embed=view.embed(), view=view)
            return
        ends = time.time() + events.EVENT_MINUTES * 60
        await channel.send(embed=embeds.event_embed(event, ends, self.timezone))
        self.active = (event, ends)
        log.info("Server event started: %s", event.key)

    @tasks.loop(minutes=events.CHECK_MINUTES)
    async def event_loop(self) -> None:
        """Now and then, while nothing is running, start an event in the announcement channel."""
        channel_id = self.settings.lottery_announce_channel_id
        # An event nobody can see is pointless, so without a channel only the
        # owner's $startevent starts them.
        if channel_id is None or self.current() is not None:
            return
        if self.rng.randrange(events.START_ODDS):
            return
        try:
            channel = await self.resolve_channel(channel_id)
            if channel is not None:
                await self.start(events.pick(self.rng), channel)
        except Exception:
            # A failed start must not kill the loop, or events stop for good.
            log.exception("Starting a server event failed; will try again next interval")

    @event_loop.before_loop
    async def _before_event_loop(self) -> None:
        """Wait for the gateway before posting anything."""
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug("No gateway connection; server events will not run")
            self.event_loop.cancel()

    @commands.hybrid_command(name="event")  # type: ignore[arg-type]
    async def event_command(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Show the server event running right now, if there is one."""
        event = self.current()
        if event is None or self.active is None:
            await ctx.send(
                "No event is running. They start without warning, so keep an eye on the "
                "announcements."
            )
            return
        await ctx.send(embed=embeds.event_embed(event, self.active[1], self.timezone))


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Events(bot))
