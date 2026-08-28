"""Builders for the embeds and message strings the bot sends.

Keeping presentation here means the cogs stay focused on rules, and the bot has
one place that decides what a balance or an error looks like.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from flyconomy import economy
from flyconomy.database import Account, LeaderboardEntry

#: The bot's brand color, carried over from version 1.
BRAND_COLOR = discord.Color(0x13FF00)

#: Color for refusals and failures.
ERROR_COLOR = discord.Color(0xFF4444)


def now(timezone: str) -> datetime:
    """Return the current time in ``timezone``, for embed footers."""
    return datetime.now(ZoneInfo(timezone))


def money(amount: int) -> str:
    """Format a dollar amount with a thousands separator, such as ``$1,000``."""
    return f"${amount:,}"


def coins(amount: int) -> str:
    """Format a Flyxcoin amount with a thousands separator."""
    return f"{amount:,}"


def balance_embed(user: discord.abc.User, account: Account, timezone: str) -> discord.Embed:
    """Build the embed for the ``balance`` command.

    Args:
        user: The member whose balance is shown.
        account: Their current balances.
        timezone: IANA timezone for the embed timestamp.

    Returns:
        A populated embed.
    """
    embed = discord.Embed(
        title=f"{user.display_name}'s Balance",
        color=BRAND_COLOR,
        timestamp=now(timezone),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Wallet:", value=money(account.wallet), inline=True)
    embed.add_field(name="Bank:", value=money(account.bank), inline=True)
    embed.add_field(name="Flyxcoin:", value=coins(account.crypto), inline=False)
    embed.add_field(name="Miner Level:", value=f"Level {account.miner}", inline=True)
    embed.add_field(name="Total Net Worth", value=money(account.net_worth), inline=False)
    return embed


def leaderboard_embed(
    title: str,
    description: str,
    entries: list[LeaderboardEntry],
    timezone: str,
) -> discord.Embed:
    """Build a ranked listing embed.

    Args:
        title: Embed title.
        description: One line of context shown above the rankings.
        entries: Ranked entries, highest first.
        timezone: IANA timezone for the embed timestamp.

    Returns:
        A populated embed, or one saying the board is empty.
    """
    if not entries:
        body = "Nobody has any money yet."
    else:
        body = "\n".join(
            f"{rank}. {money(entry.amount)} - <@{entry.user_id}>"
            for rank, entry in enumerate(entries, start=1)
        )
    return discord.Embed(
        title=title,
        description=f"{description}\n\n{body}",
        color=BRAND_COLOR,
        timestamp=now(timezone),
    )


def circulation_embed(total: int, timezone: str) -> discord.Embed:
    """Build the embed shown by ``flx`` with no action.

    Args:
        total: Flyxcoin in circulation.
        timezone: IANA timezone for the embed timestamp.

    Returns:
        A populated embed.
    """
    embed = discord.Embed(
        title="Total Flyxcoin in circulation.",
        color=BRAND_COLOR,
        timestamp=now(timezone),
    )
    embed.add_field(name="Total FLX in circulation:", value=coins(total), inline=False)
    embed.add_field(
        name="Total value of all circulating FLX:",
        value=money(economy.flx_cost(total)),
    )
    return embed


def error_embed(message: str) -> discord.Embed:
    """Build the embed used for refusals and unexpected failures."""
    return discord.Embed(description=message, color=ERROR_COLOR)
