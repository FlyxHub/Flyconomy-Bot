"""Banking, income, and leaderboard commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from flyconomy import economy, embeds
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog


class Economy(BaseCog, name="Economy"):
    """Wallet and bank management, passive income, and rankings."""

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

    @commands.hybrid_command(name="daily")  # type: ignore[arg-type]
    @commands.cooldown(1, economy.DAILY_COOLDOWN_SECONDS, commands.BucketType.user)
    async def daily(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Collect a daily payout worth 10% of your bank balance."""
        account = await self.db.get_account(ctx.author.id)
        payout = economy.daily_payout(account.bank, self.settings.max_daily_payout)
        if payout:
            await self.db.add_bank(ctx.author.id, payout)
        await ctx.send(f"You received your daily payout of **{embeds.money(payout)}**")

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
        """Delete your own account, resetting you to a new player."""
        deleted = await self.db.delete_account(ctx.author.id)
        if deleted:
            await ctx.send("Your account has been reset.")
        else:
            await ctx.send("You don't have an account to reset.")

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
