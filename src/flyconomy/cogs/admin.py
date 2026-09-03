"""Owner-only maintenance commands.

These stay classic prefix commands rather than slash commands. A slash command
is published to every member's command picker, so an owner-only slash command
advertises itself to people who can never run it.
"""

from __future__ import annotations

import re

import discord
from discord.ext import commands

from flyconomy import economy, embeds
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog

#: A raw snowflake or a mention of one. Deliberately looser than discord.py's
#: own mention converters, which require a plausible 15-20 digit snowflake: the
#: rows worth purging by hand are the impossible ones, such as user 1, that no
#: real member could ever own and that no member converter will resolve.
_USER_REFERENCE = re.compile(r"<@!?(\d+)>|(\d+)")


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

    @commands.command(name="purge")
    async def purge(self, ctx: commands.Context[FlyconomyBot], user: str) -> None:
        """Delete a user id from every table, by id rather than by member.

        ``reset`` takes a real member, so it cannot touch a row belonging to an
        id that no longer resolves to anyone — a bogus id from an old bug, or a
        member Discord no longer knows about. This takes the id itself, either
        bare or as a mention, and never looks it up.

        Args:
            ctx: Invocation context.
            user: A user id, or a mention of one.

        Raises:
            commands.BadArgument: If the argument holds no id.
        """
        match = _USER_REFERENCE.fullmatch(user.strip())
        if match is None:
            raise commands.BadArgument(f"{user!r} is not a user id or a mention.")
        user_id = int(match.group(1) or match.group(2))

        result = await self.db.purge_user(user_id)
        if not result.found:
            await ctx.send(f"`{user_id}` is not in the database.")
            return

        removed = []
        if result.account:
            removed.append("their account")
        if result.lottery_entries == 1:
            removed.append("1 lottery entry")
        elif result.lottery_entries:
            removed.append(f"{result.lottery_entries} lottery entries")
        await ctx.send(f"Purged `{user_id}`, removing {' and '.join(removed)}.")

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

    @commands.command(name="draw")
    async def draw(self, ctx: commands.Context[FlyconomyBot]) -> None:
        """Run a lottery draw now, instead of waiting for the schedule."""
        cog = self.bot.get_cog("Lottery")
        if cog is None:  # pragma: no cover - the extension is always loaded
            await ctx.send("The lottery is not loaded.")
            return

        winner, amount = await cog.run_draw()  # type: ignore[attr-defined]
        if winner is None:
            await ctx.send(f"Nobody entered, so the pot rolls over at {embeds.money(amount)}.")
            return
        await ctx.send(f"<@{winner}> won {embeds.money(amount)}!")


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Admin(bot))
