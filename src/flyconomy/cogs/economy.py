"""Banking, income, and leaderboard commands."""

from __future__ import annotations

import datetime
import logging
import time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from flyconomy import economy, embeds
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog
from flyconomy.database import ResetOutcome

log = logging.getLogger(__name__)


def _reset_seed_line(outcome: ResetOutcome) -> str:
    """Describe what the fresh account was seeded with."""
    if outcome.seed:
        return f"You start again with {embeds.money(outcome.seed)} in the bank."
    return "You start again with nothing: you have reset too many times in a row for a stake."


def _reset_next_line(outcome: ResetOutcome) -> str:
    """Warn what resetting again right now would be worth, before they do it.

    Always names the way back to a full stake, because the schedule is only
    fair if a member can see how to escape it.
    """
    hours = round(outcome.full_seed_in / 3600)
    back_to_full = (
        f"Leave it {hours} hours and your next reset is worth "
        f"{embeds.money(economy.STARTING_BANK)} again."
    )
    if outcome.next_seed:
        return (
            f"-# Reset again now and you would get {embeds.money(outcome.next_seed)}. "
            f"{back_to_full}"
        )
    return f"-# Resetting again now would get you nothing. {back_to_full}"


class Economy(BaseCog, name="Economy"):
    """Wallet and bank management, passive income, and rankings."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog and start the daily interest timer."""
        super().__init__(bot)
        hour, minute = (int(part) for part in bot.settings.daily_payout_time.split(":"))
        self._payout_time = datetime.time(hour, minute, tzinfo=ZoneInfo(bot.settings.timezone))
        self.interest_loop.change_interval(time=self._payout_time)
        self.interest_loop.start()

    async def cog_unload(self) -> None:
        """Stop the interest timer when the extension is unloaded."""
        self.interest_loop.cancel()

    # ------------------------------------------------------ daily interest --

    async def pay_daily_interest(self) -> None:
        """Pay one day's interest to every account, for today.

        Does nothing if today has already been paid, which is what makes this
        safe to call from both the scheduled tick and the startup catch-up.
        """
        today = datetime.datetime.now(ZoneInfo(self.timezone)).date().isoformat()
        paid = await self.db.pay_daily_interest(today, self.settings.max_daily_payout)
        if paid is None:
            log.debug("Daily interest for %s was already paid", today)
            return
        log.info(
            "Daily interest for %s paid %d across %d accounts",
            paid.day,
            paid.total,
            paid.accounts,
        )

    @tasks.loop(hours=24)
    async def interest_loop(self) -> None:
        """Pay the daily interest on the configured schedule."""
        try:
            await self.pay_daily_interest()
        except Exception:
            # A failed run must not kill the loop, or interest stops for good.
            log.exception("Daily interest run failed; will try again next interval")

    @interest_loop.before_loop
    async def _before_interest_loop(self) -> None:
        """Wait for the gateway, then catch up on today if the tick was missed.

        Only *today* is ever caught up, and only when the scheduled time has
        already passed. Backfilling every missed day would turn a week-long
        outage into a week of interest paid in one lump, which is the one shape
        the season's linear-growth bound does not survive; skipping the catch-up
        entirely would instead make every restart a silent tax on everyone.

        Starting before the scheduled time does nothing at all -- the loop's own
        first tick pays that day -- so the two paths cannot both fire, and
        ``pay_daily_interest`` would refuse the second one anyway.
        """
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug("No gateway connection; the daily interest timer will not run")
            self.interest_loop.cancel()
            return

        now = datetime.datetime.now(ZoneInfo(self.timezone))
        scheduled_today = now.replace(
            hour=self._payout_time.hour,
            minute=self._payout_time.minute,
            second=0,
            microsecond=0,
        )
        if now >= scheduled_today:
            await self.pay_daily_interest()

    @commands.hybrid_command(name="balance", aliases=["bal"])  # type: ignore[arg-type]
    @app_commands.describe(member="Whose balance to show. Defaults to you.")
    async def balance(
        self, ctx: commands.Context[FlyconomyBot], member: discord.Member | None = None
    ) -> None:
        """Check your balance."""
        target = member or ctx.author
        account = await self.db.get_account(target.id)
        await ctx.send(embed=embeds.balance_embed(target, account, self.timezone))

    @commands.hybrid_command(name="deposit", aliases=["dep"])  # type: ignore[arg-type]
    @app_commands.describe(amount="Dollars to deposit. Defaults to your whole wallet.")
    async def deposit(
        self,
        ctx: commands.Context[FlyconomyBot],
        amount: commands.Range[int, 1] | None = None,
    ) -> None:
        """Deposit money from your wallet into your bank account."""
        account = await self.db.get_account(ctx.author.id)
        amount = amount or account.wallet
        if not amount:
            await ctx.send("Your wallet is empty, so there is nothing to deposit.")
            return

        await self.db.transfer(ctx.author.id, source="wallet", destination="bank", amount=amount)
        await ctx.send(f"Successfully deposited {embeds.money(amount)}")

    @commands.hybrid_command(name="withdraw")  # type: ignore[arg-type]
    @app_commands.describe(amount="Dollars to withdraw. Defaults to your whole bank balance.")
    async def withdraw(
        self,
        ctx: commands.Context[FlyconomyBot],
        amount: commands.Range[int, 1] | None = None,
    ) -> None:
        """Withdraw money from your bank account into your wallet."""
        account = await self.db.get_account(ctx.author.id)
        amount = amount or account.bank
        if not amount:
            await ctx.send("Your bank account is empty, so there is nothing to withdraw.")
            return

        await self.db.transfer(ctx.author.id, source="bank", destination="wallet", amount=amount)
        await ctx.send(f"Successfully withdrawn {embeds.money(amount)}")

    @commands.hybrid_command(name="pay", aliases=["transfer"])  # type: ignore[arg-type]
    @app_commands.describe(
        member="Who receives the money.",
        amount="Dollars to send from your bank. A transfer tax is withheld.",
    )
    async def pay(
        self,
        ctx: commands.Context[FlyconomyBot],
        member: discord.Member,
        amount: commands.Range[int, economy.MIN_TRANSFER],
    ) -> None:
        """Send money from your bank to another member's, minus a transfer tax."""
        if member.id == ctx.author.id:
            await ctx.send("You cannot pay yourself.")
            return

        split = economy.split_transfer(amount, self.settings.transfer_tax_rate)
        await self.db.pay(
            ctx.author.id,
            member.id,
            split,
            creator_id=self.settings.creator_tax_user_id,
        )
        await ctx.send(
            f"You sent {embeds.money(split.net)} to {member.mention}. "
            f"{embeds.money(split.tax)} was withheld as transfer tax."
        )

    @commands.hybrid_command(name="beg")  # type: ignore[arg-type]
    @commands.cooldown(1, economy.BEG_COOLDOWN_SECONDS, commands.BucketType.user)
    async def beg(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Beg the economy gods for a small amount of money."""
        if self.rng.randint(1, economy.BEG_SUCCESS_ODDS) == 1:
            await ctx.send("You got nothing.")
            return

        amount = self.rng.randint(economy.BEG_MIN, economy.BEG_MAX)
        await self.db.add_wallet(ctx.author.id, amount)
        await ctx.send(f"You got {embeds.money(amount)}")

    @commands.hybrid_command(name="rob")  # type: ignore[arg-type]
    @commands.cooldown(1, economy.ROB_COOLDOWN_SECONDS, commands.BucketType.user)
    @app_commands.describe(member="Whose wallet to rob.")
    async def rob(self, ctx: commands.Context[FlyconomyBot], member: discord.Member) -> None:
        """Attempt to rob someone for the money in their wallet."""
        if member.id == ctx.author.id:
            await ctx.send("You cannot rob yourself.")
            ctx.command.reset_cooldown(ctx)  # type: ignore[union-attr]
            return

        victim = await self.db.get_account(member.id)
        if victim.wallet <= 0:
            await ctx.send("You can't rob someone with no money in their wallet.")
            ctx.command.reset_cooldown(ctx)  # type: ignore[union-attr]
            return

        if not economy.roll_rob(victim.security, self.rng):
            defended = (
                f" Their wallet security is at level {victim.security}." if victim.security else ""
            )
            await ctx.send(f"Robbery attempt failed.{defended} Try again in an hour.")
            return

        amount = self.rng.randint(1, victim.wallet)
        await self.db.steal(ctx.author.id, member.id, amount)
        await ctx.send(f"You robbed {embeds.money(amount)} from {member.mention}")

    @commands.hybrid_command(name="secure", aliases=["security"])  # type: ignore[arg-type]
    async def secure(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Upgrade your wallet's security, paying from your bank balance."""
        account = await self.db.get_account(ctx.author.id)
        cost = economy.security_cost(account.security)
        if cost is None:
            await ctx.send("Your wallet security is already at the maximum level.")
            return

        if account.bank < cost:
            await ctx.send(
                f"Upgrading to security level {account.security + 1} costs "
                f"{embeds.money(cost)} from your bank, but you only have "
                f"{embeds.money(account.bank)}."
            )
            return

        bought = await self.db.buy_security_upgrade(ctx.author.id)
        if bought is None:  # pragma: no cover - only when two upgrades race
            await ctx.send("Your wallet security is already at the maximum level.")
            return

        level, paid = bought
        chance = economy.rob_success_percent(level)
        await ctx.send(
            f"Wallet security upgraded to level {level} for {embeds.money(paid)}! "
            f"A robbery against you now succeeds {chance}% of the time."
        )

    @commands.hybrid_command(name="leaderboard", aliases=["lb"])  # type: ignore[arg-type]
    async def leaderboard(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Show the richest members by total net worth."""
        entries = await self.db.top_net_worth()
        await ctx.send(
            embed=embeds.leaderboard_embed(
                title=f"Top {economy.LEADERBOARD_SIZE} Richest Users",
                description="Based on total net worth",
                entries=entries,
                timezone=self.timezone,
            )
        )

    @commands.hybrid_command(name="resetme")  # type: ignore[arg-type]
    async def resetme(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Start over from nothing. Resets in a row seed less and less."""
        if await self.db.find_account(ctx.author.id) is None:
            await ctx.send("You don't have an account to reset.")
            return

        outcome = await self.db.reset_account(ctx.author.id, time.time())
        seeded = _reset_seed_line(outcome)
        await ctx.send(f"Your account has been reset. {seeded}\n{_reset_next_line(outcome)}")

    @commands.hybrid_command(name="wallets")  # type: ignore[arg-type]
    async def wallets(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Show the largest undeposited wallets, which are the best robbery targets."""
        entries = await self.db.top_wallets()
        await ctx.send(
            embed=embeds.leaderboard_embed(
                title="Top undeposited wallets",
                description="Cash left in a wallet can be stolen.",
                entries=entries,
                timezone=self.timezone,
            )
        )


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Economy(bot))
