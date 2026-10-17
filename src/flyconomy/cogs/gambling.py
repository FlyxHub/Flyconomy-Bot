"""Casino games.

Every game follows the same flow: the stake is debited from the wallet before
the outcome is rolled, and a win credits the stake multiplied by the game's
return. Debiting first means a member cannot bet money they do not have, even if
they fire two commands at once.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

# The mines module is aliased because the command's own argument is called
# `mines` -- that is the word the game uses for the count, and the member sees
# the parameter name in the slash command, so the argument keeps it.
from flyconomy import blackjack, crash, economy, embeds, tictactoe
from flyconomy import mines as mines_rules
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog
from flyconomy.economy import Card, SlotSymbol
from flyconomy.errors import BetTooLargeError, FlyconomyError
from flyconomy.views import (
    BlackjackView,
    CrashView,
    JackpotView,
    MatchChallengeView,
    MatchView,
    MinesView,
    TicTacToeView,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Bet[T]:
    """How one instant wager came out, for a command or the dashboard to describe.

    Attributes:
        stake: Dollars staked.
        multiplier: Stake multiplier returned, zero for a loss.
        outcome: What the game drew -- a coin side, a roll, the reels.
    """

    stake: int
    multiplier: int
    outcome: T

    @property
    def paid(self) -> int:
        """Dollars credited back."""
        return self.stake * self.multiplier


_ROULETTE_HELP = (
    "Bet on `red`, `black`, or a single pocket (`0`, `00`, or `1`-`36`). "
    "A color pays 2x the stake and a single pocket pays 35x."
)


class Gambling(BaseCog, name="Casino"):
    """Wager wallet money on games of chance."""

    def __init__(self, bot: FlyconomyBot) -> None:
        """Bind the cog and start with no jackpot round in play."""
        super().__init__(bot)
        # One jackpot round at a time, server-wide, the same way there is one
        # lottery draw. The lock covers the read-then-open sequence below, so
        # two members racing to start a round cannot end up with two live views
        # drawing from the same pot.
        self._live_jackpot: JackpotView | None = None
        self._jackpot_lock = asyncio.Lock()
        # Every mines board in play, for the owner's dashboard. Weak, so a
        # settled board drops out on its own once discord.py lets it go.
        self.live_mines: weakref.WeakSet[MinesView] = weakref.WeakSet()
        # When each member may spin slots again, by the monotonic clock. Kept
        # here rather than on the command so a spin from the dashboard waits
        # out the same five minutes as one from Discord.
        self._slots_ready: dict[int, float] = {}

    async def cog_load(self) -> None:
        """Hand back every wager the last shutdown left in play.

        Money for a live game lives in the database while the thing that
        decides it -- a jackpot's timer, a game board -- lives in a view
        in memory. Anything still open at startup therefore belongs to a game
        nobody is left to finish, so it all goes back.
        """
        refunded = await self.db.refund_jackpot()
        if refunded:
            log.info("Refunded %d jackpot ante(s) left open by a restart", len(refunded))

        holds = await self.db.refund_all_escrow()
        if holds:
            log.info("Refunded %d match stake(s) left held by a restart", len(holds))

        wagers = await self.db.refund_all_wagers()
        if wagers:
            log.info("Refunded %d board stake(s) left in play by a restart", wagers)

    async def cog_unload(self) -> None:
        """Drop a live jackpot round, leaving its antes for the startup refund."""
        if self._live_jackpot is not None:
            self._live_jackpot.cancel()
            self._live_jackpot = None

    def _check_limit(self, bet: int) -> None:
        """Refuse a wager above the table limit.

        Called before the stake is debited, so a refused bet costs nothing.

        Args:
            bet: The proposed stake.

        Raises:
            BetTooLargeError: If the stake exceeds the configured table limit.
        """
        if bet > self.settings.max_bet:
            raise BetTooLargeError(bet, self.settings.max_bet)

    async def _stake(self, user_id: int, bet: int) -> float | None:
        """Check the table limit, then debit the stake.

        Every game places its bet through here, so a new game cannot forget the
        limit.

        Args:
            user_id: The player.
            bet: Dollars to stake.

        Returns:
            The reset marker the payout must be credited under -- see
            :meth:`Database.stake`.

        Raises:
            BetTooLargeError: If the stake exceeds the table limit.
            InsufficientFundsError: If the wallet cannot cover the stake.
        """
        self._check_limit(bet)
        return await self.db.stake(user_id, bet)

    async def _hold(self, user_id: int, bet: int) -> tuple[int, float | None]:
        """:meth:`_stake` for a game played across button presses.

        The stake is recorded as a held wager, so a restart before the board
        settles refunds it at startup instead of destroying it.

        Returns:
            The wager's id for the view, and the reset marker.
        """
        self._check_limit(bet)
        return await self.db.hold_wager(user_id, bet)

    async def _settle(
        self,
        user_id: int,
        bet: int,
        multiplier: int,
        reset_marker: float | None,
    ) -> None:
        """Credit a win if there is one, and rake the house's take into the pot.

        Every game calls this exactly once per wager, including on a loss with a
        multiplier of zero, so the rake and the weekly tournament's score see
        the full picture rather than only the losses.

        Args:
            user_id: The player.
            bet: The stake, already debited.
            multiplier: Stake multiplier to return. Zero for a loss.
            reset_marker: What :meth:`_stake` returned for this wager.

        Raises:
            FlyconomyError: If the member reset their account while the bet
                was in play, so the win was forfeited rather than paid into
                the fresh account. Raised after the rake, which keeps it all.
        """
        paid = bet * multiplier
        await self.db.record_house_play(user_id, bet, paid)
        if paid and not await self.db.pay_winnings(user_id, paid, reset_marker):
            await self.rake(bet)
            raise FlyconomyError(embeds.VOIDED_LINE)
        await self.rake(bet - paid)

    async def _post(
        self,
        ctx: commands.Context[FlyconomyBot],
        view: BlackjackView | CrashView | MinesView,
        bet: int,
    ) -> discord.Message:
        """Send a game's board, handing the stake back if it never lands.

        A view's timeout only starts once its message is sent, so a failed send
        would leave the stake debited with nothing left to settle or refund it.

        Args:
            ctx: Invocation context, used to identify the player.
            view: The game to post, already holding the debited stake.
            bet: The stake to refund if the send fails.

        Returns:
            The posted message.
        """
        try:
            return await ctx.send(embed=view.embed(), view=view)
        except discord.HTTPException:
            await self.db.pay_winnings(ctx.author.id, bet, view.reset_marker, view.wager_id)
            view.stop()
            raise

    @commands.hybrid_command(name="coinflip", aliases=["cf"])  # type: ignore[arg-type]
    @app_commands.describe(guess="Which side you are betting on.", bet="Dollars to stake.")
    @app_commands.choices(
        guess=[
            app_commands.Choice(name="Heads", value="heads"),
            app_commands.Choice(name="Tails", value="tails"),
        ]
    )
    async def coinflip(
        self, ctx: commands.Context[FlyconomyBot], guess: str, bet: commands.Range[int, 1]
    ) -> None:
        """Bet on a coin flip. A correct call returns 2x your stake."""
        choice = guess.strip().lower()
        if choice not in economy.COIN_SIDES:
            await ctx.send("Invalid guess. Try `heads` or `tails`.")
            return

        result = await self.play_coinflip(ctx.author.id, choice, bet)
        if result.multiplier:
            await ctx.send(f"It's **{result.outcome}**. You win **{embeds.money(result.paid)}**")
        else:
            await ctx.send(f"It's **{result.outcome}**, you lose **{embeds.money(bet)}**")

    async def play_coinflip(self, user_id: int, guess: str, bet: int) -> Bet[str]:
        """Stake, flip, and settle: ``coinflip`` for the command and the dashboard alike.

        ``guess`` must already be one of :data:`economy.COIN_SIDES`.
        """
        marker = await self._stake(user_id, bet)
        flip = self.rng.choice(economy.COIN_SIDES)
        won = economy.COINFLIP_RETURN if guess == flip else 0
        await self._settle(user_id, bet, won, marker)
        return Bet(bet, won, flip)

    @commands.hybrid_command(name="rps")  # type: ignore[arg-type]
    @app_commands.describe(guess="Your move.", bet="Dollars to stake.")
    @app_commands.choices(
        guess=[
            app_commands.Choice(name="Rock", value="rock"),
            app_commands.Choice(name="Paper", value="paper"),
            app_commands.Choice(name="Scissors", value="scissors"),
        ]
    )
    async def rps(
        self, ctx: commands.Context[FlyconomyBot], guess: str, bet: commands.Range[int, 1]
    ) -> None:
        """Play rock paper scissors for money. A win returns 3x your stake."""
        move = guess.strip().lower()
        if move not in economy.RPS_MOVES:
            await ctx.send("Invalid guess. You must play `rock`, `paper`, or `scissors`.")
            return

        result = await self.play_rps(ctx.author.id, move, bet)
        bot_move = result.outcome
        match economy.rps_outcome(move, bot_move):
            case "tie":
                await ctx.send(
                    f"The bot chose **{bot_move}**. It's a tie, and the house takes ties. "
                    f"You lose **{embeds.money(bet)}**"
                )
            case "win":
                await ctx.send(
                    f"The bot chose **{bot_move}**. You win **{embeds.money(result.paid)}!**"
                )
            case _:
                await ctx.send(f"The bot chose **{bot_move}**. You lose **{embeds.money(bet)}**")

    async def play_rps(self, user_id: int, move: str, bet: int) -> Bet[str]:
        """Stake, throw, and settle. The outcome is the bot's move.

        ``move`` must already be one of :data:`economy.RPS_MOVES`.
        """
        marker = await self._stake(user_id, bet)
        bot_move = self.rng.choice(economy.RPS_MOVES)
        match economy.rps_outcome(move, bot_move):
            case "tie":
                multiplier = economy.RPS_TIE_RETURN
            case "win":
                multiplier = economy.RPS_RETURN
            case _:
                multiplier = 0
        await self._settle(user_id, bet, multiplier, marker)
        return Bet(bet, multiplier, bot_move)

    @commands.hybrid_command(name="dice")  # type: ignore[arg-type]
    @app_commands.describe(guess="The face you expect, 1 to 6.", bet="Dollars to stake.")
    async def dice(
        self,
        ctx: commands.Context[FlyconomyBot],
        guess: commands.Range[int, 1, economy.DICE_SIDES],
        bet: commands.Range[int, 1],
    ) -> None:
        """Bet on a six-sided dice roll. A correct call returns 6x your stake."""
        result = await self.play_dice(ctx.author.id, guess, bet)
        if result.multiplier:
            await ctx.send(
                f"You rolled a **{result.outcome}**. You win **{embeds.money(result.paid)}**"
            )
        else:
            await ctx.send(f"You rolled a **{result.outcome}**. You lose **{embeds.money(bet)}**")

    async def play_dice(self, user_id: int, guess: int, bet: int) -> Bet[int]:
        """Stake, roll, and settle. The outcome is the face rolled."""
        marker = await self._stake(user_id, bet)
        roll = self.rng.randint(1, economy.DICE_SIDES)
        won = economy.DICE_RETURN if guess == roll else 0
        await self._settle(user_id, bet, won, marker)
        return Bet(bet, won, roll)

    @commands.hybrid_command(name="slots", aliases=["slot"])  # type: ignore[arg-type]
    @app_commands.describe(bet="Dollars to stake.")
    async def slots(self, ctx: commands.Context[FlyconomyBot], bet: commands.Range[int, 1]) -> None:
        """Spin the slot machine. Three of a kind returns up to 55x your stake."""
        result = await self.play_slots(ctx.author.id, bet)
        reels, multiplier = result.outcome, result.multiplier

        window = " ".join(symbol.emoji for symbol in reels)
        if not multiplier:
            await ctx.send(f"[ {window} ]\nNo match. You lose **{embeds.money(bet)}**")
            return

        if all(symbol == reels[0] for symbol in reels):
            headline = f"Three {reels[0].name}!"
        else:
            # A pair, since the all-equal case is handled above.
            paired = next(s for s in reels if s.pays_on_pair and reels.count(s) == 2)
            headline = f"A pair of {paired.name}!"

        await ctx.send(f"[ {window} ]\n{headline} You win **{embeds.money(result.paid)}**")

    def slots_wait(self, user_id: int) -> float:
        """Seconds until ``user_id`` may spin again, zero when they may now."""
        return max(0.0, self._slots_ready.get(user_id, 0.0) - time.monotonic())

    async def play_slots(self, user_id: int, bet: int) -> Bet[tuple[SlotSymbol, ...]]:
        """Spin once, if the member's cooldown allows it.

        Slots is the one game with a timer (see ``SLOTS_COOLDOWN_SECONDS``).
        The window is claimed before the stake is awaited, so two spins racing
        each other cannot both get through, and handed back if the stake is
        refused: a refused bet costs nothing, and that includes the cooldown.

        Raises:
            commands.CommandOnCooldown: If the member spun too recently.
        """
        wait = self.slots_wait(user_id)
        if wait:
            raise commands.CommandOnCooldown(
                commands.Cooldown(1, economy.SLOTS_COOLDOWN_SECONDS),
                wait,
                commands.BucketType.user,
            )
        self._slots_ready[user_id] = time.monotonic() + economy.SLOTS_COOLDOWN_SECONDS
        try:
            marker = await self._stake(user_id, bet)
        except FlyconomyError:
            del self._slots_ready[user_id]
            raise
        reels = economy.spin_slots(self.rng)
        multiplier = economy.slots_payout_multiplier(reels)
        await self._settle(user_id, bet, multiplier, marker)
        return Bet(bet, multiplier, reels)

    @commands.hybrid_command(name="blackjack", aliases=["bj"])  # type: ignore[arg-type]
    @app_commands.describe(bet="Dollars to stake.")
    async def blackjack_command(
        self, ctx: commands.Context[FlyconomyBot], bet: commands.Range[int, 1]
    ) -> None:
        """Play a hand of blackjack against the dealer. A natural pays 3:2."""
        view = await self.deal_blackjack(ctx.author, bet)
        # A natural for either side decides the hand on the deal, so there is
        # nothing to press and the buttons never appear.
        if view.is_finished():
            await ctx.send(embed=view.embed())
            return
        view.message = await self._post(ctx, view, bet)

    async def deal_blackjack(self, player: discord.abc.User, bet: int) -> BlackjackView:
        """Stake and deal a hand, settling it now if a natural decided it.

        The one way a hand starts, shared by ``$blackjack`` and the dashboard
        so the web cannot drift from the table limit or the held wager.

        Raises:
            BetTooLargeError: If the stake exceeds the table limit.
            InsufficientFundsError: If the wallet cannot cover the stake.
        """
        wager, marker = await self._hold(player.id, bet)
        view = BlackjackView(
            db=self.db,
            game=blackjack.Game.deal(bet, self.rng),
            player=player,
            base_bet=bet,
            timezone=self.timezone,
            rake=self.settings.lottery_rake,
            creator_tax_rate=self.settings.creator_tax_rate,
            creator_tax_user_id=self.settings.creator_tax_user_id,
            reset_marker=marker,
        )
        view.wager_id = wager
        await view.settle()
        return view

    @commands.hybrid_command(name="crash")  # type: ignore[arg-type]
    @app_commands.describe(bet="Dollars to stake.")
    async def crash_command(
        self, ctx: commands.Context[FlyconomyBot], bet: commands.Range[int, 1]
    ) -> None:
        """Cash out before the multiplier crashes. The longer you wait, the more it pays."""
        view = await self.deal_crash(ctx.author, bet)
        # An instant bust decides the round on the deal, so there is nothing
        # to press and the button never appears.
        if view.is_finished():
            await ctx.send(embed=view.embed())
            return
        view.message = await self._post(ctx, view, bet)
        view.start_ticking()

    async def deal_crash(self, player: discord.abc.User, bet: int) -> CrashView:
        """Stake and deal a round, settling an instant bust on the spot.

        Shared by ``$crash`` and the dashboard. The caller starts the tick,
        once the round has somewhere to be drawn.
        """
        wager, marker = await self._hold(player.id, bet)
        game = crash.Game.deal(bet, self.rng)
        view = CrashView(
            db=self.db,
            game=game,
            player=player,
            timezone=self.timezone,
            rake=self.settings.lottery_rake,
            creator_tax_rate=self.settings.creator_tax_rate,
            creator_tax_user_id=self.settings.creator_tax_user_id,
            reset_marker=marker,
        )
        view.wager_id = wager
        if game.crash_point <= 1.0:
            await view.settle(multiplier=0.0)
        return view

    @commands.hybrid_command(name="mines")  # type: ignore[arg-type]
    @app_commands.describe(
        bet="Dollars to stake.",
        mines=f"How many mines to hide, {mines_rules.MIN_MINES}-{mines_rules.MAX_MINES}. "
        f"More mines pay faster. Default {mines_rules.DEFAULT_MINES}.",
    )
    async def mines_command(
        self,
        ctx: commands.Context[FlyconomyBot],
        bet: commands.Range[int, 1],
        mines: commands.Range[int, mines_rules.MIN_MINES, mines_rules.MAX_MINES] = (
            mines_rules.DEFAULT_MINES
        ),
    ) -> None:
        """Turn over tiles for a rising multiplier. Cash out before you hit a mine."""
        view = await self.deal_mines(ctx.author, bet, mines)
        view.message = await self._post(ctx, view, bet)

    async def deal_mines(self, player: discord.abc.User, bet: int, mines: int) -> MinesView:
        """Stake and deal a board, shared by ``$mines`` and the dashboard.

        Raises:
            BetTooLargeError: If the stake exceeds the table limit.
            InsufficientFundsError: If the wallet cannot cover the stake.
        """
        wager, marker = await self._hold(player.id, bet)
        game = mines_rules.Game.deal(bet, mines, self.rng)
        # Logged at deal time, before a tile is pressed, so the board can be
        # read while the round is still live. That is the point of it and also
        # its whole cost: anyone reading the log during those
        # DECISION_TIMEOUT_SECONDS plays a solved board -- every safe tile,
        # then Cash Out -- which is the one way this game pays better than its
        # 3% edge. Keep the log where only the host can see it.
        log.info(
            "mines board for %s: stake=%d mines=%s safe=%s",
            player.id,
            bet,
            sorted(game.mine_tiles),
            sorted(set(range(mines_rules.TILES)) - game.mine_tiles),
        )

        view = MinesView(
            db=self.db,
            game=game,
            player=player,
            timezone=self.timezone,
            rake=self.settings.lottery_rake,
            creator_tax_rate=self.settings.creator_tax_rate,
            creator_tax_user_id=self.settings.creator_tax_user_id,
            reset_marker=marker,
        )
        view.wager_id = wager
        self.live_mines.add(view)
        return view

    @commands.hybrid_command(name="jackpot", aliases=["jp"])  # type: ignore[arg-type]
    @app_commands.describe(ante="Dollars to ante into the pot.")
    async def jackpot_command(
        self, ctx: commands.Context[FlyconomyBot], ante: commands.Range[int, 1]
    ) -> None:
        """Ante into a shared pot. One entrant wins it all, and a bigger ante wins more often."""
        # Checked here too, so an oversized ante gets the table-limit message
        # rather than being sent back as the round's own refusal below.
        self._check_limit(ante)
        try:
            live, opened = await self.ante_jackpot(ctx.author.id, ante, ctx)
        except FlyconomyError as exc:
            # A refused join is the round's answer, not an error to translate.
            await ctx.send(str(exc))
            return
        if not opened:
            await ctx.send(
                f"You anted {embeds.money(ante)} into round #{live.state.round_number}. "
                f"The pot is {embeds.money(live.state.pot)} across "
                f"{live.state.entrants:,} entrants."
            )

    @property
    def live_jackpot(self) -> JackpotView | None:
        """The round in play, or the last one until another opens."""
        return self._live_jackpot

    async def ante_jackpot(
        self, user_id: int, ante: int, channel: discord.abc.Messageable | None
    ) -> tuple[JackpotView, bool]:
        """Join the round in play, or open one in ``channel`` if none is.

        Shared by ``$jackpot`` and the dashboard. The page has no channel of
        its own, so it passes the announcement channel, or ``None`` when
        there is none to open a round in.

        Not routed through _stake: the ante and the entry have to move in one
        transaction, so the debit happens inside enter_jackpot instead. The
        table limit still has to be checked here, before anything is charged.

        Returns:
            The round, and whether this ante opened it.

        Raises:
            BetTooLargeError: If the ante exceeds the table limit.
            FlyconomyError: If the join was refused, or there is no round and
                nowhere to open one.
        """
        self._check_limit(ante)
        async with self._jackpot_lock:
            live = self._live_jackpot
            if live is not None and not live.is_finished():
                problem = await live.apply_join(user_id, ante)
                if problem is not None:
                    raise FlyconomyError(problem)
                await live.redraw()
                return live, False
            if channel is None:
                raise FlyconomyError("No round is open. Start one in Discord with `/jackpot`.")
            return await self._open_jackpot(channel, user_id, ante), True

    async def _open_jackpot(
        self, channel: discord.abc.Messageable, user_id: int, ante: int
    ) -> JackpotView:
        """Open a round on this member's ante and start its countdown.

        Args:
            channel: Where the round's message goes.
            user_id: The member opening it.
            ante: The opening ante, already checked against the table limit.
                It is also what the Join button costs everyone else, which is
                what keeps a press inside the table limit too.
        """
        # An open round with no live view can only be one a restart or a
        # cancelled loop left behind, so hand those antes back rather than
        # letting a new round inherit them.
        if (await self.db.jackpot_state()).entries:
            refunded = await self.db.refund_jackpot()
            log.warning("Refunded %d orphaned jackpot ante(s)", len(refunded))

        await self.db.enter_jackpot(user_id, ante)
        view = JackpotView(
            db=self.db,
            rng=self.rng,
            state=await self.db.jackpot_state(),
            ante=ante,
            timezone=self.timezone,
            rake=self.settings.lottery_rake,
            creator_tax_rate=self.settings.creator_tax_rate,
            creator_tax_user_id=self.settings.creator_tax_user_id,
            limiter=self.limiter,
        )
        try:
            view.message = await channel.send(embed=view.embed(), view=view)
        except discord.HTTPException:
            # No round went up to settle the ante, so hand it straight back.
            await self.db.refund_jackpot()
            raise
        view.start_ticking()
        self._live_jackpot = view
        return view

    def _match_builder(
        self, view_class: Callable[..., MatchView], new_game: Callable[[], object], bet: int
    ) -> Callable[[int, tuple[discord.abc.User, discord.abc.User]], MatchView]:
        """Return a factory that turns an accepted challenge into a match.

        The challenge view holds no payout settings of its own: they are closed
        over here instead, so a new head-to-head game only has to say which
        view and which board it wants.

        Args:
            view_class: The match view to build.
            new_game: Builds this game's opening position.
            bet: What each player is staking.

        Returns:
            A callable taking the escrow's id and the seated players.
        """

        def build(hold_id: int, players: tuple[discord.abc.User, discord.abc.User]) -> MatchView:
            return view_class(
                db=self.db,
                game=new_game(),
                players=players,
                hold_id=hold_id,
                bet=bet,
                timezone=self.timezone,
                rake=self.settings.lottery_rake,
                creator_tax_rate=self.settings.creator_tax_rate,
                creator_tax_user_id=self.settings.creator_tax_user_id,
            )

        return build

    async def _offer_match(
        self,
        ctx: commands.Context[FlyconomyBot],
        *,
        opponent: discord.Member | None,
        bet: int,
        game_name: str,
        title: str,
        lapses_in: float,
        payout: Callable[[int], int],
        view_class: Callable[..., MatchView],
        new_game: Callable[[], object],
    ) -> None:
        """Post a challenge to a head-to-head match.

        Nothing is staked here: both stakes are taken together when the
        challenge is accepted, so a challenge nobody answers costs nothing and
        there is no held money to refund. The table limit is still checked
        first, because the accept button stakes exactly this amount.

        Args:
            ctx: Invocation context, used to identify the challenger.
            opponent: Who to challenge, or ``None`` to leave it open.
            bet: Dollars each player stakes.
            game_name: Which game this is, recorded against the escrow.
            title: What to call the game in the embed.
            lapses_in: Seconds before an unanswered challenge lapses.
            payout: This game's payout for a given pot, shown on the offer.
            view_class: The match view to build on acceptance.
            new_game: Builds the game's opening position.
        """
        self._check_limit(bet)
        if opponent is not None:
            if opponent.id == ctx.author.id:
                await ctx.send("You cannot play yourself.")
                return
            if opponent.bot:
                await ctx.send(f"Bots do not play {title}.")
                return

        view = MatchChallengeView(
            db=self.db,
            rng=self.rng,
            game_name=game_name,
            title=title,
            payout=payout,
            build_match=self._match_builder(view_class, new_game, bet),
            challenger=ctx.author,
            challenged=opponent,
            bet=bet,
            timezone=self.timezone,
            timeout=lapses_in,
            limiter=self.limiter,
        )
        view.message = await ctx.send(embed=view.embed(), view=view)

    @commands.hybrid_command(name="tictactoe", aliases=["ttt"])  # type: ignore[arg-type]
    @app_commands.describe(
        opponent="Who to challenge. Leave it out to let anyone accept.",
        bet="Dollars each of you stakes.",
    )
    async def tictactoe_command(
        self,
        ctx: commands.Context[FlyconomyBot],
        opponent: discord.Member | None,
        bet: commands.Range[int, 1],
    ) -> None:
        """Challenge someone to tic-tac-toe. Best of three boards, winner takes the pot."""
        await self._offer_match(
            ctx,
            opponent=opponent,
            bet=bet,
            game_name="tictactoe",
            title="Tic-tac-toe",
            lapses_in=tictactoe.CHALLENGE_TIMEOUT_SECONDS,
            payout=tictactoe.payout,
            view_class=TicTacToeView,
            new_game=tictactoe.Game.new,
        )

    @commands.hybrid_command(name="war")  # type: ignore[arg-type]
    @app_commands.describe(bet="Dollars to stake.")
    async def war(self, ctx: commands.Context[FlyconomyBot], bet: commands.Range[int, 1]) -> None:
        """Draw a card against the dealer. The higher card wins, and a tie is returned."""
        result = await self.play_war(ctx.author.id, bet)
        player, dealer = result.outcome

        draw = f"You drew **{player}**, the dealer drew **{dealer}**."
        match result.multiplier:
            case economy.WAR_WIN_RETURN:
                await ctx.send(f"{draw} You win **{embeds.money(result.paid)}**")
            case economy.WAR_TIE_RETURN:
                await ctx.send(f"{draw} It's a tie, so your {embeds.money(bet)} is returned.")
            case _:
                await ctx.send(f"{draw} You lose **{embeds.money(bet)}**")

    async def play_war(self, user_id: int, bet: int) -> Bet[tuple[Card, Card]]:
        """Stake, draw a card each, and settle. The outcome is (yours, the dealer's)."""
        marker = await self._stake(user_id, bet)
        player, dealer = economy.draw_cards(2, self.rng)
        multiplier = economy.war_payout_multiplier(player, dealer)
        await self._settle(user_id, bet, multiplier, marker)
        return Bet(bet, multiplier, (player, dealer))

    @commands.hybrid_command(name="roulette")  # type: ignore[arg-type]
    @app_commands.describe(bet=_ROULETTE_HELP, amount="Dollars to stake.")
    async def roulette(
        self,
        ctx: commands.Context[FlyconomyBot],
        bet: str,
        amount: commands.Range[int, 1],
    ) -> None:
        """Bet on a spin of an American roulette wheel."""
        wager = economy.parse_roulette_bet(bet)
        if wager is None:
            await ctx.send(f"That is not a valid bet. {_ROULETTE_HELP}")
            return

        # Settled before anything is sent, so a failed send cannot strand a win.
        result = await self.play_roulette(ctx.author.id, wager, amount)
        await ctx.send(f"And the roll is..... **{result.outcome}**")
        if result.multiplier:
            await ctx.send(
                f"Congratulations {ctx.author.mention}, you won **{embeds.money(result.paid)}**"
            )
        else:
            await ctx.send("Sorry, you lost your bet.")

    async def play_roulette(self, user_id: int, wager: int | str, amount: int) -> Bet[int | str]:
        """Stake, spin, and settle. ``wager`` is what ``economy.parse_roulette_bet`` returned."""
        marker = await self._stake(user_id, amount)
        pocket = self.rng.choice(economy.ROULETTE_WHEEL)
        multiplier = economy.roulette_payout_multiplier(wager, pocket)
        await self._settle(user_id, amount, multiplier, marker)
        return Bet(amount, multiplier, pocket)


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Gambling(bot))
