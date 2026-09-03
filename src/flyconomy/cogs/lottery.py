"""The lottery: a pot that redistributes money instead of creating it.

Two rules keep it from becoming an exploit, and both are load bearing:

- **Every entrant has exactly one entry.** Odds cannot be bought, so no amount
  of play buys a larger claim on the pot.
- **The pot is fed by the house's net take, not by gross losses.** Gross losses
  on a fair game are unbounded and nearly free, so anything paid out in
  proportion to them can be farmed by churning coinflip. The house's net take
  from a fair game is zero, so churning it contributes nothing.

The pot never creates money. Entry fees are members' own money held in escrow,
and the rake is a share of what the house already won.
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from flyconomy import embeds
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog

log = logging.getLogger(__name__)


class Lottery(BaseCog, name="Lottery"):
    """Enter the draw, and run it on a schedule."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog and start the draw timer."""
        super().__init__(bot)
        hour, minute = (int(part) for part in bot.settings.lottery_draw_time.split(":"))
        draw_time = datetime.time(hour, minute, tzinfo=ZoneInfo(bot.settings.timezone))
        self.draw_loop.change_interval(time=draw_time)
        self.draw_loop.start()

    async def cog_unload(self) -> None:
        """Stop the draw timer when the extension is unloaded."""
        self.draw_loop.cancel()

    # ---------------------------------------------------------- the draw ----

    async def run_draw(self) -> tuple[int | None, int]:
        """Run one draw.

        Picks one entrant uniformly at random and pays them the whole pot. With
        nobody entered the pot rolls over untouched, which is what lets a
        jackpot build on a quiet server.

        Returns:
            A ``(winner_id, amount)`` pair. The winner is ``None`` when the draw
            rolled over, and the amount is then the pot carried forward.
        """
        entrants = await self.db.lottery_entrants()
        if not entrants:
            carried = await self.db.roll_over_lottery()
            log.info("Lottery rolled over with no entrants; pot is now %d", carried)
            return None, carried

        winner = self.rng.choice(entrants)
        amount = await self.db.award_lottery(winner)
        log.info("Lottery paid %d to %s across %d entrants", amount, winner, len(entrants))
        return winner, amount

    @tasks.loop(hours=24)
    async def draw_loop(self) -> None:
        """Run the draw on the configured schedule."""
        try:
            await self.run_draw()
        except Exception:
            # A failed draw must not kill the loop, or the pot never pays out.
            log.exception("Lottery draw failed; will try again next interval")

    @draw_loop.before_loop
    async def _before_draw_loop(self) -> None:
        """Wait until the bot is connected before the first draw.

        A client that never logged in has no gateway to wait on, which is the
        case in unit tests and when startup failed. There is nothing to schedule
        against then, so the timer stops rather than drawing into the void.
        """
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug("No gateway connection; the lottery timer will not run")
            self.draw_loop.cancel()

    # ---------------------------------------------------------- commands ----

    @commands.hybrid_group(name="lottery", fallback="info", invoke_without_command=True)  # type: ignore[arg-type]
    async def lottery(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Show the lottery pot and how to enter."""
        state = await self.db.lottery_state()
        entered = await self.db.has_entered(ctx.author.id)

        embed = discord.Embed(
            title=f"Lottery draw #{state.draw}",
            color=embeds.BRAND_COLOR,
            timestamp=embeds.now(self.timezone),
        )
        embed.add_field(name="Pot", value=embeds.money(state.pot), inline=True)
        embed.add_field(name="Entrants", value=f"{state.entrants:,}", inline=True)
        embed.add_field(
            name="Ticket", value=embeds.money(self.settings.lottery_ticket_price), inline=True
        )
        odds = "-" if state.entrants == 0 else f"1 in {state.entrants:,}"
        embed.add_field(name="Your odds if you enter now", value=odds, inline=False)
        embed.add_field(name="You", value="Entered" if entered else "Not entered", inline=False)
        embed.set_footer(text="One entry each. Everyone entered has the same chance.")
        await ctx.send(embed=embed)

    @lottery.command(name="enter")  # type: ignore[arg-type]
    async def lottery_enter(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Enter the current draw. One entry per member, paid from your bank."""
        price = self.settings.lottery_ticket_price
        entered = await self.db.enter_lottery(ctx.author.id, price)
        if not entered:
            await ctx.send("You are already in this draw. Everyone gets exactly one entry.")
            return

        state = await self.db.lottery_state()
        await ctx.send(
            f"You are in draw #{state.draw} for {embeds.money(price)}. "
            f"The pot is {embeds.money(state.pot)} across {state.entrants:,} entrants."
        )

    @lottery.command(name="entrants")  # type: ignore[arg-type]
    async def lottery_entrants(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """List who is in the current draw."""
        entrants = await self.db.lottery_entrants()
        if not entrants:
            await ctx.send("Nobody has entered this draw yet.")
            return

        shown = ", ".join(f"<@{user_id}>" for user_id in entrants[:25])
        more = f" and {len(entrants) - 25:,} more" if len(entrants) > 25 else ""
        await ctx.send(f"{len(entrants):,} entered: {shown}{more}")


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Lottery(bot))
