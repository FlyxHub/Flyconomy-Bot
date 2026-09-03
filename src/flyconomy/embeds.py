"""Builders for the embeds and message strings the bot sends.

Keeping presentation here means the cogs stay focused on rules, and the bot has
one place that decides what a balance or an error looks like.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from flyconomy import blackjack, crash, economy
from flyconomy.database import Account, LeaderboardEntry
from flyconomy.economy import Card

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


def flx_ticker(price: int, previous: int) -> str:
    """Render a short price-and-trend string, such as ``$10,340 ▲2.1%``.

    Shared by the bot's status and the ``flx`` info embed, so the two always
    agree on how a move reads.

    Args:
        price: The current price.
        previous: The price before the move. Zero reads as no prior price,
            which is shown flat rather than dividing by zero.

    Returns:
        The formatted ticker string.
    """
    if not previous:
        return money(price)
    change = (price - previous) / previous * 100
    arrow = "\N{BLACK UP-POINTING TRIANGLE}" if change > 0 else "\N{BLACK DOWN-POINTING TRIANGLE}"
    if change == 0:
        arrow = "\N{BLACK RIGHT-POINTING TRIANGLE}"
    return f"{money(price)} {arrow}{change:+.1f}%"


def circulation_embed(total: int, price: int, timezone: str) -> discord.Embed:
    """Build the embed shown by ``flx`` with no action.

    Args:
        total: Flyxcoin in circulation.
        price: The live Flyxcoin price.
        timezone: IANA timezone for the embed timestamp.

    Returns:
        A populated embed.
    """
    embed = discord.Embed(
        title="Total Flyxcoin in circulation.",
        color=BRAND_COLOR,
        timestamp=now(timezone),
    )
    embed.add_field(name="Current FLX price:", value=money(price), inline=False)
    embed.add_field(name="Total FLX in circulation:", value=coins(total), inline=False)
    embed.add_field(
        name="Total value of all circulating FLX:",
        value=money(economy.flx_cost(total, price)),
    )
    return embed


def lottery_winner_embed(winner_id: int, amount: int, draw: int, timezone: str) -> discord.Embed:
    """Build the embed announcing a lottery draw's winner.

    Args:
        winner_id: The member who won.
        amount: The pot paid out.
        draw: The draw number that was decided.
        timezone: IANA timezone for the embed timestamp.

    Returns:
        A populated embed.
    """
    embed = discord.Embed(
        title=f"Lottery draw #{draw}",
        description=f"<@{winner_id}> won {money(amount)}!",
        color=BRAND_COLOR,
        timestamp=now(timezone),
    )
    return embed


def error_embed(message: str) -> discord.Embed:
    """Build the embed used for refusals and unexpected failures."""
    return discord.Embed(description=message, color=ERROR_COLOR)


def hand(cards: Sequence[Card]) -> str:
    """Render a hand of cards as ``A♠ 10♥``."""
    return " ".join(str(card) for card in cards)


def blackjack_result_line(game: blackjack.Game) -> str:
    """Describe how a finished hand ended, and what it paid.

    Args:
        game: A finished hand.

    Returns:
        One line for the embed footer field.

    Raises:
        ValueError: If the hand has not finished.
    """
    if game.outcome is None:
        msg = "the hand is still in play"
        raise ValueError(msg)

    won = blackjack.payout(game.stake, game.outcome)
    match game.outcome:
        case blackjack.Outcome.PLAYER_BLACKJACK:
            return f"**Blackjack!** You win {money(won)}"
        case blackjack.Outcome.DEALER_BUST:
            return f"Dealer busts. You win {money(won)}"
        case blackjack.Outcome.PLAYER_WINS:
            return f"You win {money(won)}"
        case blackjack.Outcome.PUSH:
            return f"Push. Your {money(game.stake)} is returned."
        case blackjack.Outcome.PLAYER_BUST:
            return f"Bust. You lose {money(game.stake)}"
        case _:
            return f"Dealer wins. You lose {money(game.stake)}"


def blackjack_embed(game: blackjack.Game, user: discord.abc.User, timezone: str) -> discord.Embed:
    """Build the embed for a hand of blackjack.

    The dealer's second card stays face down until the hand is decided, so the
    embed is safe to post while the player is still choosing.

    Args:
        game: The hand, finished or in play.
        user: The player.
        timezone: IANA timezone for the embed timestamp.

    Returns:
        A populated embed.
    """
    finished = game.finished
    if finished:
        colour = ERROR_COLOR if game.outcome is not None and game.outcome.is_loss else BRAND_COLOR
    else:
        colour = BRAND_COLOR

    embed = discord.Embed(
        title=f"Blackjack - {user.display_name}",
        color=colour,
        timestamp=now(timezone),
    )

    if finished:
        dealer_line = f"{hand(game.dealer)}  (**{game.dealer_value}**)"
    else:
        # Only the upcard is public while the player is still deciding.
        dealer_line = f"{game.dealer_upcard} ??"
    embed.add_field(name="Dealer", value=dealer_line, inline=False)

    player_line = f"{hand(game.player)}  (**{game.player_value}**)"
    if not finished and blackjack.is_soft(game.player):
        player_line += "  *soft*"
    embed.add_field(name="You", value=player_line, inline=False)

    stake_label = "Stake (doubled)" if game.doubled else "Stake"
    embed.add_field(name=stake_label, value=money(game.stake), inline=True)

    if finished:
        embed.add_field(name="Result", value=blackjack_result_line(game), inline=False)
    else:
        embed.set_footer(text="Hit, stand, or double down.")
    return embed


def crash_result_line(
    game: crash.Game, *, elapsed: float, cashed_out_multiplier: float | None
) -> str:
    """Describe how a round of crash ended, or its live state if it hasn't.

    Args:
        game: The round.
        elapsed: Seconds since the round started.
        cashed_out_multiplier: The multiplier the player locked in, or
            ``None`` if they have not cashed out.

    Returns:
        One line for the embed's result field, or a prompt if still live.
    """
    if cashed_out_multiplier is not None:
        won = crash.payout(game.stake, cashed_out_multiplier)
        return f"Cashed out at **{cashed_out_multiplier:.2f}x**. You win {money(won)}"
    if crash.has_crashed(game, elapsed):
        return f"**Crashed at {game.crash_point:.2f}x!** You lose {money(game.stake)}"
    return "Cash out before it crashes!"


def crash_embed(
    game: crash.Game,
    user: discord.abc.User,
    timezone: str,
    *,
    elapsed: float,
    cashed_out_multiplier: float | None,
) -> discord.Embed:
    """Build the embed for a round of crash, live or decided.

    Args:
        game: The round.
        user: The player.
        timezone: IANA timezone for the embed timestamp.
        elapsed: Seconds since the round started.
        cashed_out_multiplier: The multiplier the player locked in, or
            ``None`` if they have not cashed out.

    Returns:
        A populated embed.
    """
    busted = crash.has_crashed(game, elapsed)
    finished = cashed_out_multiplier is not None or busted
    colour = ERROR_COLOR if finished and cashed_out_multiplier is None else BRAND_COLOR

    embed = discord.Embed(
        title=f"Crash - {user.display_name}",
        color=colour,
        timestamp=now(timezone),
    )
    embed.add_field(
        name="Multiplier", value=f"{crash.current_multiplier(game, elapsed):.2f}x", inline=True
    )
    embed.add_field(name="Stake", value=money(game.stake), inline=True)

    if finished:
        embed.add_field(
            name="Result",
            value=crash_result_line(
                game, elapsed=elapsed, cashed_out_multiplier=cashed_out_multiplier
            ),
            inline=False,
        )
    else:
        embed.set_footer(text="Cash out before it crashes.")
    return embed
