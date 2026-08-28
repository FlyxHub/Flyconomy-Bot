"""Owner-only maintenance commands.

These stay classic prefix commands rather than slash commands. A slash command
is published to every member's command picker, so an owner-only slash command
advertises itself to people who can never run it.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from flyconomy import economy
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog


class Admin(BaseCog, name="Admin"):
    """Commands restricted to the bot owner."""

    async def cog_check(self, ctx: commands.Context[FlyconomyBot]) -> bool:  # type: ignore[override]
        """Restrict every command in this cog to the bot owner.

        Args:
            ctx: Invocation context.

        Returns:
            ``True`` if the caller owns the bot.

        Raises:
            commands.NotOwner: If the caller is not the owner.
        """
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner
        return True

    @commands.command(name="adminme")
    async def adminme(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Give yourself the admin miner, which always mines the maximum yield."""
        await self.db.set_miner_level(ctx.author.id, economy.ADMIN_MINER_LEVEL)
        await ctx.send(f"Miner set to level {economy.ADMIN_MINER_LEVEL}.")

    @commands.command(name="adminmine")
    async def adminmine(self, ctx: commands.Context[FlyconomyBot], amount: int) -> None:
        """Mint Flyxcoin into your own account.

        Args:
            ctx: Invocation context.
            amount: Coins to add. A negative amount removes them.
        """
        await self.db.add_crypto(ctx.author.id, amount)
        await ctx.send(f"Adjusted your Flyxcoin by {amount:,}.")

    @commands.command(name="reset")
    async def reset(self, ctx: commands.Context[FlyconomyBot], member: discord.Member) -> None:
        """Delete a member's account, which resets them to a new player.

        Args:
            ctx: Invocation context.
            member: The member to reset.
        """
        deleted = await self.db.delete_account(member.id)
        if deleted:
            await ctx.send(f"Reset {member.mention}.")
        else:
            await ctx.send(f"{member.mention} does not have an account.")

    @commands.command(name="sync")
    async def sync(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Republish slash commands to Discord.

        Run this after adding or renaming a command. A global sync can take up
        to an hour to reach every client.
        """
        async with ctx.typing():
            count = await self.bot.sync_commands()
        scope = (
            f"guild {self.settings.dev_guild_id}"
            if self.settings.dev_guild_id is not None
            else "globally"
        )
        await ctx.send(f"Synced {count} slash commands {scope}.")


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Admin(bot))
