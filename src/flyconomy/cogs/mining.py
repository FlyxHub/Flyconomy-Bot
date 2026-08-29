"""Flyxcoin mining, miner upgrades, and the Flyxcoin market."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from flyconomy import economy, embeds
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog


class Mining(BaseCog, name="Flyxcoin"):
    """Mine, upgrade, and trade Flyxcoin."""

    @commands.hybrid_command(name="mine")  # type: ignore[arg-type]
    @commands.cooldown(1, economy.MINE_COOLDOWN_SECONDS, commands.BucketType.user)
    async def mine(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Use your Flyxcoin miner to mine Flyxcoin."""
        account = await self.db.get_account(ctx.author.id)
        if account.miner == 0:
            await ctx.send("You need to buy a Flyxcoin miner to mine! Try `/upgrade`.")
            ctx.command.reset_cooldown(ctx)  # type: ignore[union-attr]
            return

        if ctx.author.id in self.settings.always_mine_user_ids:
            mined = 1
        else:
            mined = economy.roll_mine(account.miner, self.rng)

        if not mined:
            await ctx.send("You mined nothing.")
            return

        await self.db.add_crypto(ctx.author.id, mined)
        suffix = "" if mined == 1 else "s"
        await ctx.send(f"You mined {mined} Flyxcoin{suffix}!")

    @commands.hybrid_command(name="upgrade")  # type: ignore[arg-type]
    async def upgrade(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Upgrade your Flyxcoin miner, paying from your bank balance."""
        account = await self.db.get_account(ctx.author.id)
        cost = economy.upgrade_cost(account.miner)
        if cost is None:
            await ctx.send("Your miner is already at the maximum level.")
            return

        if account.bank < cost:
            await ctx.send(
                f"Upgrading to level {account.miner + 1} costs {embeds.money(cost)} "
                f"from your bank, but you only have {embeds.money(account.bank)}."
            )
            return

        level = await self.db.buy_miner_upgrade(ctx.author.id, cost)
        chance = economy.mine_chance_percent(level)
        await ctx.send(
            f"Miner upgraded to level {level} for {embeds.money(cost)}! "
            f"You have a {chance}% chance of mining a Flyxcoin."
        )

    @commands.hybrid_group(name="flx", fallback="info", invoke_without_command=True)  # type: ignore[arg-type]
    async def flx(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Show how much Flyxcoin is in circulation and its current price."""
        total = await self.db.total_crypto()
        price = await self.db.get_flx_price()
        await ctx.send(embed=embeds.circulation_embed(total, price, self.timezone))

    @flx.command(name="buy")  # type: ignore[arg-type]
    @app_commands.describe(amount="Coins to buy. Defaults to as many as you can afford.")
    async def flx_buy(
        self,
        ctx: commands.Context[FlyconomyBot],
        amount: commands.Range[int, 1] | None = None,
    ) -> None:
        """Buy Flyxcoin with money from your bank account, at the live price."""
        account = await self.db.get_account(ctx.author.id)
        amount = amount or economy.affordable_flx(account.bank, account.flx_price)
        if not amount:
            await ctx.send(
                f"One Flyxcoin costs {embeds.money(account.flx_price)} and you have "
                f"{embeds.money(account.bank)} in the bank."
            )
            return

        cost = await self.db.buy_crypto(ctx.author.id, amount)
        await ctx.send(f"You purchased {amount:,} Flyxcoin for {embeds.money(cost)}!")

    @flx.command(name="sell")  # type: ignore[arg-type]
    @app_commands.describe(amount="Coins to sell. Defaults to everything you hold.")
    async def flx_sell(
        self,
        ctx: commands.Context[FlyconomyBot],
        amount: commands.Range[int, 1] | None = None,
    ) -> None:
        """Sell Flyxcoin, crediting the proceeds to your bank account."""
        account = await self.db.get_account(ctx.author.id)
        amount = amount or account.crypto
        if not amount:
            await ctx.send("You do not have any Flyxcoin to sell.")
            return

        proceeds = await self.db.sell_crypto(ctx.author.id, amount)
        await ctx.send(f"You sold {amount:,} Flyxcoin for {embeds.money(proceeds)}")

    @flx.command(name="send")  # type: ignore[arg-type]
    @app_commands.describe(member="Who receives the coins.", amount="Coins to send.")
    async def flx_send(
        self,
        ctx: commands.Context[FlyconomyBot],
        member: discord.Member,
        amount: commands.Range[int, 1],
    ) -> None:
        """Send Flyxcoin to another member."""
        if member.id == ctx.author.id:
            await ctx.send("You cannot send Flyxcoin to yourself.")
            return

        await self.db.transfer_crypto(ctx.author.id, member.id, amount)
        await ctx.send(f"You sent {amount:,} Flyxcoin to {member.mention}")


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Mining(bot))
