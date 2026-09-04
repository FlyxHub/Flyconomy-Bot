"""Builders for the embeds and message strings the bot sends.

Keeping presentation here means the cogs stay focused on rules, and the bot has
one place that decides what a balance or an error looks like.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

import discord

from flyconomy import blackjack, crash, economy, jackpot, tictactoe
from flyconomy.database import Account, JackpotState, LeaderboardEntry
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


#: Entrants listed in full on a jackpot embed before the rest are summarized.
_JACKPOT_ENTRANTS_SHOWN = 15


def jackpot_entrants(state: JackpotState) -> str:
    """Render a jackpot round's entrants, each with the odds their ante bought.

    Args:
        state: The round to render.

    Returns:
        One line per entrant, or a prompt when nobody has anted yet.
    """
    if not state.entries:
        return "Nobody has anted yet."

    pot = state.pot
    lines = [
        f"<@{entry.user_id}> - {money(entry.amount)} ({jackpot.win_chance(entry.amount, pot):.0%})"
        for entry in state.entries[:_JACKPOT_ENTRANTS_SHOWN]
    ]
    hidden = len(state.entries) - _JACKPOT_ENTRANTS_SHOWN
    if hidden > 0:
        lines.append(f"...and {hidden:,} more")
    return "\n".join(lines)


def jackpot_result_line(
    state: JackpotState, *, winner_id: int | None, paid: int, refunded: bool
) -> str:
    """Describe how a jackpot round ended.

    Args:
        state: The round as it stood when it closed.
        winner_id: The member who took the pot, or ``None`` if nobody did.
        paid: Dollars paid to the winner, after the house's cut.
        refunded: Whether every ante was handed back instead.

    Returns:
        One line for the embed's result field.
    """
    if refunded:
        return "Not enough entrants, so nothing was drawn. Every ante was returned in full."
    if winner_id is None:  # pragma: no cover - a decided round always has one
        return "Nobody entered."
    return f"<@{winner_id}> wins {money(paid)} from a pot of {money(state.pot)}!"


def jackpot_embed(
    state: JackpotState,
    timezone: str,
    *,
    ante: int,
    seconds_left: float,
    winner_id: int | None = None,
    paid: int = 0,
    refunded: bool = False,
    finished: bool = False,
) -> discord.Embed:
    """Build the embed for a jackpot round, live or decided.

    Args:
        state: The round, as last read.
        timezone: IANA timezone for the embed timestamp.
        ante: The opening ante, which is what the Join button costs.
        seconds_left: Seconds until the round closes.
        winner_id: The member who took the pot, once decided.
        paid: Dollars paid to the winner, after the house's cut.
        refunded: Whether the round closed too small to draw and was refunded.
        finished: Whether the round has closed.

    Returns:
        A populated embed.
    """
    colour = ERROR_COLOR if refunded else BRAND_COLOR
    embed = discord.Embed(
        title=f"Jackpot round #{state.round_number}",
        color=colour,
        timestamp=now(timezone),
    )
    embed.add_field(name="Pot", value=money(state.pot), inline=True)
    embed.add_field(name="Entrants", value=f"{state.entrants:,}", inline=True)
    if not finished:
        embed.add_field(name="Closes in", value=f"{max(0, round(seconds_left))}s", inline=True)

    embed.add_field(name="In the pot", value=jackpot_entrants(state), inline=False)

    if finished:
        embed.add_field(
            name="Result",
            value=jackpot_result_line(state, winner_id=winner_id, paid=paid, refunded=refunded),
            inline=False,
        )
    else:
        embed.set_footer(
            text=(
                f"Join antes {money(ante)}. A bigger ante is a bigger share of the "
                f"pot and better odds with it."
            )
        )
    return embed


def match_challenge_embed(
    title: str,
    challenger: discord.abc.User,
    challenged: discord.abc.User | None,
    bet: int,
    timezone: str,
    *,
    winner_takes: int,
    lapses_in: float,
) -> discord.Embed:
    """Build the embed offering a head-to-head match, before either stake is taken.

    Shared by every head-to-head game, so it takes what the winner stands to
    take rather than working it out from one game's cut.

    Args:
        title: What to call the game.
        challenger: The member who called it.
        challenged: The member being challenged, or ``None`` when the offer is
            open to whoever takes it first.
        bet: Dollars each of them stakes.
        timezone: IANA timezone for the embed timestamp.
        winner_takes: What the winner will be paid.
        lapses_in: Seconds before an unanswered challenge withdraws itself.

    Returns:
        A populated embed.
    """
    takings = f"Winner takes **{money(winner_takes)}**."
    if challenged is None:
        description = (
            f"{challenger.mention} is looking for a game at **{money(bet)}** a side.\n\n"
            f"Anyone can accept. {takings}"
        )
    else:
        description = (
            f"{challenger.mention} challenges {challenged.mention} for "
            f"**{money(bet)}** each.\n\n{takings}"
        )

    embed = discord.Embed(
        title=f"{title} challenge",
        description=description,
        color=BRAND_COLOR,
        timestamp=now(timezone),
    )
    embed.set_footer(
        text=(
            "Nothing is staked until the challenge is accepted. It lapses in "
            f"{round(lapses_in)} seconds."
        )
    )
    return embed


#: What a button shows for each mark. An empty square carries a zero-width
#: space, because Discord will not take a button with no label at all.
_TICTACTOE_LABELS: Final = {
    tictactoe.EMPTY: "\N{ZERO WIDTH SPACE}",
    tictactoe.FIRST: "X",
    tictactoe.SECOND: "O",
}

#: What a button looks like for each mark, and for the line that won.
_TICTACTOE_STYLES: Final = {
    tictactoe.EMPTY: discord.ButtonStyle.secondary,
    tictactoe.FIRST: discord.ButtonStyle.danger,
    tictactoe.SECOND: discord.ButtonStyle.primary,
}


def tictactoe_label(mark: int) -> str:
    """Return the label a square's button shows."""
    return _TICTACTOE_LABELS[mark]


def tictactoe_style(mark: int, *, won: bool) -> discord.ButtonStyle:
    """Return the style a square's button takes.

    Args:
        mark: What occupies the square.
        won: Whether the square is part of the line that won the board.

    Returns:
        The button style. A winning line turns green, so the three squares
        that decided the board are obvious without reading them.
    """
    if won:
        return discord.ButtonStyle.success
    return _TICTACTOE_STYLES[mark]


def tictactoe_result_line(
    players: Sequence[discord.abc.User],
    *,
    winner: discord.abc.User | None,
    forfeited: bool,
    voided: bool,
    paid: int,
    bet: int,
    boards: int,
) -> str:
    """Describe how a match ended.

    Args:
        players: Both players.
        winner: The member who won, or ``None`` for a called-off match.
        forfeited: Whether the loser resigned or ran out of time.
        voided: Whether the match was called off by a purge.
        paid: Dollars paid to the winner.
        bet: What each player staked.
        boards: How many boards were played.

    Returns:
        One line for the embed's result field.
    """
    if voided:
        return f"The match was called off. Both stakes of {money(bet)} were returned."
    if winner is None:
        return (
            f"All {boards} boards were drawn, so nobody won. "
            f"Both stakes of {money(bet)} were returned."
        )
    loser = next((player for player in players if player.id != winner.id), None)
    how = f"{loser.mention} forfeits. " if forfeited and loser is not None else ""
    return f"{how}{winner.mention} wins {money(paid)}!"


def tictactoe_embed(
    players: Sequence[discord.abc.User],
    timezone: str,
    *,
    bet: int,
    board_number: int,
    to_move: discord.abc.User | None,
    winner: discord.abc.User | None = None,
    forfeited: bool = False,
    voided: bool = False,
    drawn: bool = False,
    paid: int = 0,
) -> discord.Embed:
    """Build the embed for a tic-tac-toe match, live or decided.

    There is deliberately no picture of the board here: the buttons are the
    board, so drawing it twice would only give the two a chance to disagree.

    Args:
        players: Both players, the one to move first on this board first.
        timezone: IANA timezone for the embed timestamp.
        bet: What each player staked.
        board_number: Which board of the match is being played.
        to_move: The player whose turn it is, or ``None`` once decided.
        winner: The member who won, once decided.
        forfeited: Whether the loser resigned or timed out.
        voided: Whether the match was called off and both stakes returned.
        drawn: Whether every board was drawn.
        paid: Dollars paid to the winner.

    Returns:
        A populated embed.
    """
    finished = winner is not None or drawn or voided
    colour = ERROR_COLOR if voided or drawn else BRAND_COLOR

    embed = discord.Embed(
        title="Tic-tac-toe",
        color=colour,
        timestamp=now(timezone),
    )
    embed.add_field(
        name="Players",
        value="\n".join(
            f"{tictactoe_label(mark)} {player.mention}"
            for mark, player in zip((tictactoe.FIRST, tictactoe.SECOND), players, strict=False)
        ),
        inline=True,
    )
    embed.add_field(name="Stake", value=f"{money(bet)} each", inline=True)
    embed.add_field(
        name="Board", value=f"{board_number} of {tictactoe.BOARDS_PER_MATCH}", inline=True
    )

    if finished:
        embed.add_field(
            name="Result",
            value=tictactoe_result_line(
                players,
                winner=winner,
                forfeited=forfeited,
                voided=voided,
                paid=paid,
                bet=bet,
                boards=board_number,
            ),
            inline=False,
        )
    elif to_move is not None:
        embed.add_field(name="To move", value=to_move.mention, inline=False)
        footer = "Take a square."
        if board_number > 1:
            footer = "That board was drawn. New board, and the first move swaps."
        embed.set_footer(text=footer)
    return embed
