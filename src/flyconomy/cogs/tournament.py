"""The weekly tournament: a player-funded pot for the best week at the casino.

The rules are in :mod:`flyconomy.tournament`. The score is kept by the casino
itself -- every house wager reports its result through
``Database.record_house_play`` as it settles -- so this cog only takes
entries and settles finished weeks.
"""

from __future__ import annotations

import datetime
import logging
import time
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from flyconomy import embeds, tournament
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog
from flyconomy.database import TournamentResult

log = logging.getLogger(__name__)


class Tournament(BaseCog, name="Tournament"):
    """Join the week's tournament, and settle each week once it is over."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog and start the settling timer, which runs at midnight."""
        super().__init__(bot)
        self._zone = ZoneInfo(bot.settings.timezone)
        self.settle_loop.change_interval(time=datetime.time(0, 0, tzinfo=self._zone))
        self.settle_loop.start()

    async def cog_unload(self) -> None:
        """Stop the settling timer when the extension is unloaded."""
        self.settle_loop.cancel()

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(self._zone)

    # ------------------------------------------------------------ settling --

    async def settle_due(self) -> list[TournamentResult]:
        """Settle every week that is over and still has entries, oldest first.

        Run nightly rather than weekly, so a bot that was down at midnight on
        Monday settles the week the next time it runs instead of a week late.
        Catching up is safe here in a way it is not for the interest: a pot is
        only ever its own fees, so settling a late week creates nothing.

        Returns:
            Every week settled by this call.
        """
        current = tournament.week_of(self._now())
        settled: list[TournamentResult] = []
        for week in await self.db.open_tournament_weeks():
            if week >= current:
                continue
            result = await self.db.settle_tournament(week, time.time())
            if result is None:
                continue
            settled.append(result)
            await self.rake(result.cut)
            log.info(
                "Tournament %s settled: winner=%s pot=%d paid=%d",
                week,
                result.winner_id,
                result.pot,
                result.paid,
            )
            await self._announce(result)
        return settled

    async def _announce(self, result: TournamentResult) -> None:
        """Post a settled week to the announcement channel, best effort.

        The money has moved by the time this runs, so an unreachable channel
        is logged rather than raised, exactly as the lottery's draw does.
        """
        channel_id = self.settings.lottery_announce_channel_id
        if channel_id is None:
            return
        channel = await self.resolve_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.send(embed=embeds.tournament_result_embed(result, self.timezone))
        except discord.HTTPException:
            log.warning("Failed to post the tournament result to channel %d", channel_id)

    @tasks.loop(hours=24)
    async def settle_loop(self) -> None:
        """Settle finished weeks on the configured schedule."""
        try:
            await self.settle_due()
        except Exception:
            # A failed settle must not kill the loop, or no week ever pays.
            log.exception("Tournament settling failed; will try again next interval")

    @settle_loop.before_loop
    async def _before_settle_loop(self) -> None:
        """Wait for the gateway, then settle anything a restart left overdue."""
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug("No gateway connection; the tournament timer will not run")
            self.settle_loop.cancel()
            return
        await self.settle_due()

    # ------------------------------------------------------------ commands --

    @commands.hybrid_group(name="tournament", fallback="info", invoke_without_command=True)  # type: ignore[arg-type]
    async def tournament_group(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Show this week's tournament: the pot, the standings, and where you stand."""
        now = self._now()
        week = tournament.week_of(now)
        pot, entrants, leaders = await self.db.tournament_standings(week)
        await ctx.send(
            embed=embeds.tournament_embed(
                pot=pot,
                entrants=entrants,
                leaders=leaders,
                ends_at=tournament.week_ends(now),
                you=await self.db.tournament_entry(week, ctx.author.id),
                last=await self.db.last_tournament_result(),
                timezone=self.timezone,
            )
        )

    @tournament_group.command(name="join")  # type: ignore[arg-type]
    async def tournament_join(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Buy into this week's tournament, paid from your bank."""
        week = tournament.week_of(self._now())
        if not await self.db.join_tournament(ctx.author.id, week, time.time()):
            await ctx.send("You are already in this week's tournament.")
            return
        await ctx.send(
            f"You are in this week's tournament for {embeds.money(tournament.ENTRY_FEE)}. "
            f"Every house game you play from now on counts. Stake at least "
            f"{embeds.money(tournament.MIN_WAGERED)} over the week to qualify to win."
        )


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Tournament(bot))
