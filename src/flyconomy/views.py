"""Interactive message components.

The button callbacks here do as little as possible: each one advances the hand
through an ``apply_*`` coroutine that takes no :class:`discord.Interaction`, then
redraws the message. Keeping the rules and the money out of the callbacks is
what makes the view testable without a gateway connection.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

import discord
from discord.ext import tasks

from flyconomy import blackjack, connect4, crash, embeds, jackpot
from flyconomy.database import Database, JackpotState
from flyconomy.errors import InsufficientFundsError
from flyconomy.ratelimit import SlidingWindowLimiter

log = logging.getLogger(__name__)


class BlackjackView(discord.ui.View):
    """Hit, stand, and double-down buttons for one hand of blackjack.

    The view owns settling the hand, and guards against paying out twice if a
    click and the timeout race each other.

    Attributes:
        game: The hand in play.
        message: The message the buttons live on, set by the caller after
            sending so the timeout can redraw it.
    """

    def __init__(
        self,
        *,
        db: Database,
        game: blackjack.Game,
        player: discord.abc.User,
        base_bet: int,
        timezone: str,
        rake: float = 0.0,
        creator_tax_rate: float = 0.0,
        creator_tax_user_id: int | None = None,
    ) -> None:
        """Build a view for a freshly dealt hand.

        Args:
            db: Open database, used to credit the payout.
            game: The dealt hand.
            player: The member who owns the hand. Nobody else may press.
            base_bet: The opening stake, which is also what a double down costs.
            timezone: IANA timezone for the embed timestamp.
            rake: Share of the house's take on this hand to send to the lottery
                pot. Signed, but a hand the player wins contributes nothing and
                never pulls the pot back down.
            creator_tax_rate: Share of the house's take on this hand to send to
                ``creator_tax_user_id``, carved out of what ``rake`` leaves for
                destruction. Ignored when that user ID is unset.
            creator_tax_user_id: Bank account credited with the creator tax, or
                ``None`` to disable it outright.
        """
        super().__init__(timeout=blackjack.DECISION_TIMEOUT_SECONDS)
        self.db = db
        self.game = game
        self.player = player
        self.base_bet = base_bet
        self.timezone = timezone
        self.rake = rake
        self.creator_tax_rate = creator_tax_rate
        self.creator_tax_user_id = creator_tax_user_id
        self.message: discord.Message | None = None
        self._settled = False
        self._refresh_buttons()

    # ----------------------------------------------------------- rendering --

    def embed(self) -> discord.Embed:
        """Return the embed for the hand as it currently stands."""
        return embeds.blackjack_embed(self.game, self.player, self.timezone)

    def _refresh_buttons(self) -> None:
        """Enable or disable each button to match the state of the hand."""
        finished = self.game.finished
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "blackjack:double":
                    child.disabled = not self.game.can_double
                else:
                    child.disabled = finished

    # ------------------------------------------------------------- actions --

    async def settle(self) -> None:
        """Credit the payout once the hand is decided.

        Safe to call more than once: only the first call after the hand
        finishes moves money.
        """
        if not self.game.finished or self._settled:
            return
        self._settled = True

        assert self.game.outcome is not None  # noqa: S101 - guarded by `finished`
        amount = blackjack.payout(self.game.stake, self.game.outcome)
        if amount:
            await self.db.add_wallet(self.player.id, amount)

        house_take = self.game.stake - amount
        share = int(house_take * self.rake)
        if share > 0:
            await self.db.add_to_pot(share)

        if self.creator_tax_user_id is not None:
            cut = int(house_take * self.creator_tax_rate)
            if cut > 0:
                await self.db.add_bank(self.creator_tax_user_id, cut)
        self.stop()

    async def apply_hit(self) -> None:
        """Draw one card, settling if that ends the hand."""
        self.game.hit()
        await self.settle()
        self._refresh_buttons()

    async def apply_stand(self) -> None:
        """Stand, play the dealer out, and settle."""
        self.game.stand()
        await self.settle()
        self._refresh_buttons()

    async def apply_double(self) -> str | None:
        """Double the stake, take one card, and settle.

        The extra stake is debited before the card is dealt, so a member who
        cannot cover it does not get a free card.

        Returns:
            ``None`` on success, or a message explaining why the hand could not
            be doubled.
        """
        if not self.game.can_double:
            return "You can only double down on your first two cards."

        try:
            await self.db.add_wallet(self.player.id, -self.base_bet)
        except InsufficientFundsError as exc:
            return (
                f"Doubling down costs another {embeds.money(self.base_bet)}, "
                f"but your wallet only holds {embeds.money(exc.available)}."
            )

        self.game.double_down()
        await self.settle()
        self._refresh_buttons()
        return None

    # -------------------------------------------------------------- events --

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Let only the member who was dealt the hand press the buttons.

        Args:
            interaction: The button press.

        Returns:
            Whether the press should be handled.
        """
        if interaction.user.id == self.player.id:
            return True
        await interaction.response.send_message(
            embed=embeds.error_embed("That is not your hand."), ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        """Stand automatically so a walked-away hand still pays out."""
        if self.game.finished:
            return
        await self.apply_stand()
        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                log.warning("Could not redraw a timed-out blackjack hand", exc_info=True)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        """Replace the message with the current state of the hand."""
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ------------------------------------------------------------- buttons --

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, custom_id="blackjack:hit")
    async def hit(
        self, interaction: discord.Interaction, _button: discord.ui.Button[BlackjackView]
    ) -> None:
        """Draw another card."""
        await self.apply_hit()
        await self._redraw(interaction)

    @discord.ui.button(
        label="Stand", style=discord.ButtonStyle.secondary, custom_id="blackjack:stand"
    )
    async def stand(
        self, interaction: discord.Interaction, _button: discord.ui.Button[BlackjackView]
    ) -> None:
        """Keep the hand and let the dealer play."""
        await self.apply_stand()
        await self._redraw(interaction)

    @discord.ui.button(
        label="Double Down", style=discord.ButtonStyle.success, custom_id="blackjack:double"
    )
    async def double(
        self, interaction: discord.Interaction, _button: discord.ui.Button[BlackjackView]
    ) -> None:
        """Double the stake and take exactly one more card."""
        problem = await self.apply_double()
        if problem is not None:
            await interaction.response.send_message(
                embed=embeds.error_embed(problem), ephemeral=True
            )
            return
        await self._redraw(interaction)


class CrashView(discord.ui.View):
    """A single Cash Out button on a round of crash.

    The round's outcome is always computed from elapsed wall-clock time, via
    the injectable ``now`` callable, rather than from whatever the last
    scheduled redraw happened to show. That is what keeps a throttled or
    skipped tick harmless: cashing out (or busting) is decided by real time,
    the tick is only ever a best-effort cosmetic redraw on top of it.

    Attributes:
        game: The round in play.
        message: The message the button lives on, set by the caller after
            sending so the tick loop and the timeout can redraw it.
    """

    def __init__(
        self,
        *,
        db: Database,
        game: crash.Game,
        player: discord.abc.User,
        timezone: str,
        rake: float = 0.0,
        creator_tax_rate: float = 0.0,
        creator_tax_user_id: int | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build a view for a freshly dealt round.

        Args:
            db: Open database, used to credit the payout.
            game: The dealt round.
            player: The member who owns the round. Nobody else may press.
            timezone: IANA timezone for the embed timestamp.
            rake: Share of the house's take on this round to send to the
                lottery pot. A round the player wins contributes nothing and
                never pulls the pot back down.
            creator_tax_rate: Share of the house's take on this round to send
                to ``creator_tax_user_id``. Ignored when that user ID is unset.
            creator_tax_user_id: Bank account credited with the creator tax,
                or ``None`` to disable it outright.
            now: Clock used to measure elapsed time, injectable so tests can
                drive a round without real delays.
        """
        super().__init__(timeout=crash.DECISION_TIMEOUT_SECONDS)
        self.db = db
        self.game = game
        self.player = player
        self.timezone = timezone
        self.rake = rake
        self.creator_tax_rate = creator_tax_rate
        self.creator_tax_user_id = creator_tax_user_id
        self.now = now
        self.started_at = self.now()
        self.message: discord.Message | None = None
        self._settled = False
        self._cashed_out_multiplier: float | None = None
        self._refresh_buttons()

    # ----------------------------------------------------------- rendering --

    def elapsed(self) -> float:
        """Seconds since the round started, per the injected clock."""
        return self.now() - self.started_at

    def embed(self) -> discord.Embed:
        """Return the embed for the round as it currently stands."""
        return embeds.crash_embed(
            self.game,
            self.player,
            self.timezone,
            elapsed=self.elapsed(),
            cashed_out_multiplier=self._cashed_out_multiplier,
        )

    def _refresh_buttons(self) -> None:
        """Disable the button once the round is settled."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = self._settled

    # ------------------------------------------------------------- actions --

    async def settle(self, *, multiplier: float) -> None:
        """Credit the payout once the round is decided.

        Safe to call more than once: only the first call moves money, so a
        cash-out racing a bust tick or a timeout can never pay twice.

        Args:
            multiplier: The multiplier to pay out at, or ``0.0`` for a bust.
        """
        if self._settled:
            return
        self._settled = True

        amount = crash.payout(self.game.stake, multiplier)
        if amount:
            await self.db.add_wallet(self.player.id, amount)

        house_take = self.game.stake - amount
        share = int(house_take * self.rake)
        if share > 0:
            await self.db.add_to_pot(share)

        if self.creator_tax_user_id is not None:
            cut = int(house_take * self.creator_tax_rate)
            if cut > 0:
                await self.db.add_bank(self.creator_tax_user_id, cut)
        self.stop()

    async def apply_cashout(self) -> str | None:
        """Lock in the multiplier at the current elapsed time and settle.

        Returns:
            ``None`` on success, or a message explaining that the round had
            already crashed by the time the press was processed.
        """
        elapsed = self.elapsed()
        if crash.has_crashed(self.game, elapsed):
            return f"Too slow — it already crashed at {self.game.crash_point:.2f}x."

        multiplier = crash.current_multiplier(self.game, elapsed)
        self._cashed_out_multiplier = multiplier
        await self.settle(multiplier=multiplier)
        self._refresh_buttons()
        return None

    def start_ticking(self) -> None:
        """Begin the live redraw loop.

        Call only after :attr:`message` is set, since the first tick may need
        to edit it.
        """
        self._tick.start()

    @tasks.loop(seconds=crash.TICK_SECONDS)
    async def _tick(self) -> None:
        """Redraw the live embed, or settle and stop once the round busts.

        Best-effort: a throttled or failed edit is logged and skipped rather
        than retried or allowed to stop the loop, since the round's outcome
        never depends on whether this redraw lands.
        """
        if self._settled:
            self._tick.stop()
            return

        elapsed = self.elapsed()
        if crash.has_crashed(self.game, elapsed):
            await self.settle(multiplier=0.0)
            self._refresh_buttons()
            self._tick.stop()

        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except discord.HTTPException:
            log.warning("Could not redraw a live crash round", exc_info=True)

    # -------------------------------------------------------------- events --

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Let only the member who started the round press the button.

        Args:
            interaction: The button press.

        Returns:
            Whether the press should be handled.
        """
        if interaction.user.id == self.player.id:
            return True
        await interaction.response.send_message(
            embed=embeds.error_embed("That is not your round."), ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        """Force a bust so a walked-away round still settles.

        A safety net only: the tick loop always settles a busted round well
        before this timeout fires, so this only matters if that loop somehow
        stopped running.
        """
        if self._tick.is_running():
            self._tick.stop()
        if self._settled:
            return
        await self.settle(multiplier=0.0)
        self._refresh_buttons()
        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                log.warning("Could not redraw a timed-out crash round", exc_info=True)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        """Replace the message with the current state of the round."""
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ------------------------------------------------------------- buttons --

    @discord.ui.button(
        label="Cash Out", style=discord.ButtonStyle.success, custom_id="crash:cashout"
    )
    async def cash_out(
        self, interaction: discord.Interaction, _button: discord.ui.Button[CrashView]
    ) -> None:
        """Lock in the current multiplier."""
        problem = await self.apply_cashout()
        if problem is not None:
            await interaction.response.send_message(
                embed=embeds.error_embed(problem), ephemeral=True
            )
            return
        await self._redraw(interaction)


class JackpotView(discord.ui.View):
    """A Join button on a live, player-funded jackpot round.

    Unlike the blackjack and crash views, this one belongs to everybody: any
    member may press Join, because the game is the other entrants rather than
    the house. The round closes on elapsed wall-clock time read through the
    injectable ``now`` callable, so a throttled or skipped redraw can neither
    extend nor shorten the window in which an ante still counts.

    Attributes:
        state: The round as last read from the database.
        message: The message the button lives on, set by the caller after
            sending so the tick loop and the timeout can redraw it.
    """

    def __init__(
        self,
        *,
        db: Database,
        rng: random.Random,
        state: JackpotState,
        ante: int,
        timezone: str,
        rake: float = 0.0,
        creator_tax_rate: float = 0.0,
        creator_tax_user_id: int | None = None,
        limiter: SlidingWindowLimiter | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build a view for a round that already holds its opening ante.

        Args:
            db: Open database, used to take antes and pay the pot out.
            rng: Random source for the draw, injectable for deterministic tests.
            state: The round as it stands, already holding the opener's ante.
            ante: The opening ante, which is what the Join button costs. The
                command that opened the round has already checked it against
                the table limit, so a press cannot stake past that limit.
            timezone: IANA timezone for the embed timestamp.
            rake: Share of the house's cut to send to the lottery pot.
            creator_tax_rate: Share of the house's cut to send to
                ``creator_tax_user_id``. Ignored when that user ID is unset.
            creator_tax_user_id: Bank account credited with the creator tax,
                or ``None`` to disable it outright.
            limiter: The shared action budget. A button press does not pass
                through ``BaseCog.cog_check``, so joining spends budget here
                instead; ``None`` disables that, which is what tests want.
            now: Clock used to measure elapsed time, injectable so tests can
                run a round without waiting out its timer.
        """
        super().__init__(timeout=jackpot.DECISION_TIMEOUT_SECONDS)
        self.db = db
        self.rng = rng
        self.state = state
        self.ante = ante
        self.timezone = timezone
        self.rake = rake
        self.creator_tax_rate = creator_tax_rate
        self.creator_tax_user_id = creator_tax_user_id
        self.limiter = limiter
        self.now = now
        self.started_at = self.now()
        self.message: discord.Message | None = None
        self.winner_id: int | None = None
        self.paid = 0
        self.refunded = False
        self._settled = False

    # ----------------------------------------------------------- rendering --

    def elapsed(self) -> float:
        """Seconds since the round opened, per the injected clock."""
        return self.now() - self.started_at

    def seconds_left(self) -> float:
        """Seconds until the round stops taking antes, never below zero."""
        return max(0.0, jackpot.ROUND_SECONDS - self.elapsed())

    def is_open(self) -> bool:
        """Whether the round is still taking antes."""
        return not self._settled and self.seconds_left() > 0

    def embed(self) -> discord.Embed:
        """Return the embed for the round as it currently stands."""
        return embeds.jackpot_embed(
            self.state,
            self.timezone,
            ante=self.ante,
            seconds_left=self.seconds_left(),
            winner_id=self.winner_id,
            paid=self.paid,
            refunded=self.refunded,
            finished=self._settled,
        )

    def _refresh_buttons(self) -> None:
        """Disable the button once the round is settled."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = self._settled

    # ------------------------------------------------------------- actions --

    async def refresh(self) -> None:
        """Re-read the round, so the embed shows every ante taken so far."""
        self.state = await self.db.jackpot_state()

    async def apply_join(self, user_id: int, amount: int) -> str | None:
        """Ante a member into the round.

        Args:
            user_id: The member joining.
            amount: Dollars to ante. The caller is responsible for checking
                this against the table limit.

        Returns:
            ``None`` on success, or a message explaining the refusal.
        """
        if not self.is_open():
            return "That round has already closed. Start a new one with `jackpot`."

        try:
            entered = await self.db.enter_jackpot(user_id, amount)
        except InsufficientFundsError as exc:
            return (
                f"An ante of {embeds.money(amount)} is more than your wallet holds "
                f"({embeds.money(exc.available)})."
            )
        if not entered:
            return "You are already in this round. Everyone antes once."

        await self.refresh()
        return None

    async def settle(self) -> None:
        """Close the round, draw a winner, and pay the pot out.

        Safe to call more than once: only the first call moves money, so the
        closing tick racing the safety-net timeout can never pay twice.

        A round that closes below ``jackpot.MIN_ENTRANTS`` is refunded in full
        rather than drawn, so nobody is charged a cut for a game that had no
        opponent in it.
        """
        if self._settled:
            return
        self._settled = True

        # Read once more before anything moves: a join landing in the same
        # moment the round closes is either in this pot or in none.
        await self.refresh()

        if self.state.entrants < jackpot.MIN_ENTRANTS:
            await self.db.refund_jackpot()
            self.refunded = True
        else:
            cut = jackpot.house_cut(self.state.pot)
            self.winner_id = jackpot.draw_winner(self.state.entries, self.rng)
            self.paid = await self.db.award_jackpot(self.winner_id, cut=cut)

            share = int(cut * self.rake)
            if share > 0:
                await self.db.add_to_pot(share)
            if self.creator_tax_user_id is not None:
                tax = int(cut * self.creator_tax_rate)
                if tax > 0:
                    await self.db.add_bank(self.creator_tax_user_id, tax)

        self._refresh_buttons()
        self.stop()

    def start_ticking(self) -> None:
        """Begin the live redraw loop.

        Call only after :attr:`message` is set, since the first tick may need
        to edit it.
        """
        self._tick.start()

    def cancel(self) -> None:
        """Drop the round without settling it, for a clean shutdown.

        The antes stay in the database, which is exactly where the startup
        refund expects to find a round nobody is left to draw.
        """
        if self._tick.is_running():
            self._tick.cancel()
        self.stop()

    @tasks.loop(seconds=jackpot.TICK_SECONDS)
    async def _tick(self) -> None:
        """Redraw the countdown, or settle and stop once the round closes.

        Best-effort: a throttled or failed edit is logged and skipped rather
        than retried, since when the round closes never depends on whether
        this redraw lands.
        """
        if self._settled:
            self._tick.stop()
            return

        if self.seconds_left() <= 0:
            await self.settle()
            self._tick.stop()
        else:
            await self.refresh()

        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except discord.HTTPException:
            log.warning("Could not redraw a live jackpot round", exc_info=True)

    # -------------------------------------------------------------- events --

    async def on_timeout(self) -> None:
        """Draw the round so a stalled tick loop still pays the pot out.

        A safety net only: the tick loop settles a closed round well before
        this fires, so this only matters if that loop stopped running. Leaving
        it unsettled would strand every ante in the database until a restart
        refunded them.
        """
        if self._tick.is_running():
            self._tick.stop()
        if self._settled:
            return
        await self.settle()
        await self.redraw()

    async def redraw(self) -> None:
        """Edit the round's message in place, best effort."""
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except discord.HTTPException:
            log.warning("Could not redraw a jackpot round", exc_info=True)

    def _spend_budget(self, user_id: int) -> str | None:
        """Charge a button press against the shared action budget.

        A press never passes through ``BaseCog.cog_check``, so without this a
        member could join every round on the server as fast as they can click.

        Args:
            user_id: The member pressing.

        Returns:
            ``None`` when the press is allowed, or a message telling them how
            long to wait.
        """
        if self.limiter is None:
            return None
        wait = self.limiter.acquire(user_id)
        if wait:
            return f"You are acting too quickly. Try again in {max(1, round(wait))} seconds."
        return None

    # ------------------------------------------------------------- buttons --

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="jackpot:join")
    async def join(
        self, interaction: discord.Interaction, _button: discord.ui.Button[JackpotView]
    ) -> None:
        """Ante the round's opening amount into the pot."""
        problem = self._spend_budget(interaction.user.id)
        if problem is None:
            problem = await self.apply_join(interaction.user.id, self.ante)
        if problem is not None:
            await interaction.response.send_message(
                embed=embeds.error_embed(problem), ephemeral=True
            )
            return
        await interaction.response.edit_message(embed=self.embed(), view=self)


class Connect4View(discord.ui.View):
    """A board of Connect 4 between two members, with a stake already held.

    Both stakes are in escrow before this view exists, so settling is a matter
    of releasing that hold: to the winner less the house's cut, or back to both
    on a draw. The match can end three ways -- a connected four, a resignation,
    and a player running out of time -- and all three come through
    :meth:`settle`, which moves money exactly once.

    The view's timeout is per move rather than per match, because discord.py
    restarts it on every interaction. A player who walks away therefore
    forfeits instead of leaving two stakes held indefinitely.

    Attributes:
        game: The board.
        players: Both players, the first mover first.
        message: The message the board lives on, set by the caller after
            sending so the timeout can redraw it.
    """

    def __init__(
        self,
        *,
        db: Database,
        game: connect4.Game,
        players: tuple[discord.abc.User, discord.abc.User],
        hold_id: int,
        bet: int,
        timezone: str,
        rake: float = 0.0,
        creator_tax_rate: float = 0.0,
        creator_tax_user_id: int | None = None,
    ) -> None:
        """Build a view for a match whose stakes are already held.

        Args:
            db: Open database, used to release the escrowed stakes.
            game: The board, usually empty.
            players: Both players, the first mover first.
            hold_id: The escrow holding both stakes.
            bet: What each player staked.
            timezone: IANA timezone for the embed timestamp.
            rake: Share of the house's cut to send to the lottery pot.
            creator_tax_rate: Share of the house's cut to send to
                ``creator_tax_user_id``. Ignored when that user ID is unset.
            creator_tax_user_id: Bank account credited with the creator tax,
                or ``None`` to disable it outright.
        """
        super().__init__(timeout=connect4.MOVE_TIMEOUT_SECONDS)
        self.db = db
        self.game = game
        self.players = players
        self.hold_id = hold_id
        self.bet = bet
        self.timezone = timezone
        self.rake = rake
        self.creator_tax_rate = creator_tax_rate
        self.creator_tax_user_id = creator_tax_user_id
        self.message: discord.Message | None = None
        self.winner: discord.abc.User | None = None
        self.forfeited = False
        self.voided = False
        self.drawn = False
        self.paid = 0
        self._settled = False

        for column in range(connect4.COLUMNS):
            # Five buttons to a row is Discord's limit, so seven columns take
            # two rows and the resignation sits below them.
            self.add_item(_ColumnButton(column, row=column // 5))
        self._refresh_buttons()

    # ----------------------------------------------------------- rendering --

    @property
    def to_move(self) -> discord.abc.User:
        """The player whose turn it is."""
        return self.players[self.game.turn - 1]

    def embed(self) -> discord.Embed:
        """Return the embed for the match as it currently stands."""
        return embeds.connect4_embed(
            self.game,
            self.players,
            self.timezone,
            bet=self.bet,
            winner=self.winner,
            forfeited=self.forfeited,
            voided=self.voided,
            drawn=self.drawn,
            paid=self.paid,
        )

    def _refresh_buttons(self) -> None:
        """Match each button to the board: a full column cannot be played."""
        for child in self.children:
            if isinstance(child, _ColumnButton):
                child.disabled = self._settled or not self.game.can_drop(child.column)
            elif isinstance(child, discord.ui.Button):
                child.disabled = self._settled

    def opponent_of(self, user: discord.abc.User) -> discord.abc.User:
        """Return the other player.

        Args:
            user: One of the two players.

        Returns:
            The other one.
        """
        first, second = self.players
        return second if user.id == first.id else first

    def is_player(self, user_id: int) -> bool:
        """Return whether a member is in this match."""
        return any(player.id == user_id for player in self.players)

    # ------------------------------------------------------------- actions --

    async def apply_drop(self, user_id: int, column: int) -> str | None:
        """Drop the moving player's disc, settling the match if that ends it.

        Args:
            user_id: The member pressing.
            column: Column index, from zero on the left.

        Returns:
            ``None`` on success, or a message explaining the refusal.
        """
        if self._settled:
            return "That match is already over."
        if user_id != self.to_move.id:
            return f"It is {self.to_move.display_name}'s turn."
        if not self.game.can_drop(column):
            return f"Column {column + 1} is full."

        self.game.drop(column)
        if self.game.finished:
            await self.settle()
        self._refresh_buttons()
        return None

    async def apply_resign(self, user_id: int) -> str | None:
        """Concede the match, handing the pot to the other player.

        Args:
            user_id: The member resigning.

        Returns:
            ``None`` on success, or a message explaining the refusal.
        """
        if self._settled:
            return "That match is already over."
        resigning = next(player for player in self.players if player.id == user_id)
        await self.settle(forfeited_by=resigning)
        return None

    async def settle(self, *, forfeited_by: discord.abc.User | None = None) -> None:
        """Release the escrowed stakes, once.

        Safe to call more than once: only the first call moves money, so a
        resignation racing the move timeout can never pay out twice.

        Args:
            forfeited_by: The player who resigned or ran out of time, if the
                match ended that way rather than on the board.
        """
        if self._settled:
            return
        self._settled = True

        if forfeited_by is not None:
            self.forfeited = True
            self.winner = self.opponent_of(forfeited_by)
        elif self.game.winner is not None:
            self.winner = self.players[self.game.winner - 1]

        if self.winner is None:
            # A drawn board: both stakes go back untouched, the same as a push
            # at blackjack or a tie at war.
            returned = await self.db.refund_escrow(self.hold_id)
            self.voided = returned == 0
            self.drawn = not self.voided
        else:
            cut = connect4.house_cut(self.bet * 2)
            self.paid = await self.db.settle_escrow(self.hold_id, winner_id=self.winner.id, cut=cut)
            if self.paid == 0:
                # The hold was gone, so a purge voided this match while it was
                # being played and both stakes have already gone back.
                self.voided = True
                self.winner = None
                self.forfeited = False
            else:
                share = int(cut * self.rake)
                if share > 0:
                    await self.db.add_to_pot(share)
                if self.creator_tax_user_id is not None:
                    tax = int(cut * self.creator_tax_rate)
                    if tax > 0:
                        await self.db.add_bank(self.creator_tax_user_id, tax)

        self._refresh_buttons()
        self.stop()

    # -------------------------------------------------------------- events --

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Let only the two players press anything.

        Whose turn it is is checked in the callback instead, so that a player
        can always resign, including on their opponent's turn.

        Args:
            interaction: The button press.

        Returns:
            Whether the press should be handled.
        """
        if self.is_player(interaction.user.id):
            return True
        await interaction.response.send_message(
            embed=embeds.error_embed("That is not your match."), ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        """Forfeit for whoever was to move, so a walked-away match still pays.

        Without this the two stakes would sit in escrow until the next
        restart, which is the one thing a held wager must never do.
        """
        if self._settled:
            return
        await self.settle(forfeited_by=self.to_move)
        await self.redraw()

    async def redraw(self) -> None:
        """Edit the match's message in place, best effort."""
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except discord.HTTPException:
            log.warning("Could not redraw a Connect 4 match", exc_info=True)

    # ------------------------------------------------------------- buttons --

    @discord.ui.button(
        label="Resign", style=discord.ButtonStyle.danger, custom_id="connect4:resign", row=2
    )
    async def resign(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Connect4View]
    ) -> None:
        """Concede the match."""
        problem = await self.apply_resign(interaction.user.id)
        if problem is not None:
            await interaction.response.send_message(
                embed=embeds.error_embed(problem), ephemeral=True
            )
            return
        await interaction.response.edit_message(embed=self.embed(), view=self)


class _ColumnButton(discord.ui.Button["Connect4View"]):
    """One column of the board.

    Seven near-identical decorated callbacks would say nothing seven times, so
    the column is carried on the button instead and the callback stays as thin
    as the others: it calls an ``apply_*`` coroutine and redraws.
    """

    def __init__(self, column: int, *, row: int) -> None:
        """Build the button for one column.

        Args:
            column: Column index, from zero on the left.
            row: Which action row to place it on.
        """
        super().__init__(
            label=str(column + 1),
            style=discord.ButtonStyle.primary,
            custom_id=f"connect4:drop:{column}",
            row=row,
        )
        self.column = column

    async def callback(self, interaction: discord.Interaction) -> None:
        """Drop the pressing player's disc into this column."""
        view = self.view
        assert view is not None  # noqa: S101 - a button always has its view
        problem = await view.apply_drop(interaction.user.id, self.column)
        if problem is not None:
            await interaction.response.send_message(
                embed=embeds.error_embed(problem), ephemeral=True
            )
            return
        await interaction.response.edit_message(embed=view.embed(), view=view)


class Connect4ChallengeView(discord.ui.View):
    """Accept and decline buttons on a Connect 4 challenge.

    A challenge is either aimed at one member or left open for whoever takes it
    first. Nothing is staked while this view is up: both stakes are taken in
    one transaction the moment it is accepted, and the board replaces this view
    on the same message. A challenge that lapses or is turned down therefore
    has nothing to refund.

    An open challenge can be pressed by several members at once, so accepting
    is serialized behind a lock. Without it two presses could both get past the
    "still open" check while the first was still awaiting its escrow, and the
    challenger would be staked twice for one seat.

    Attributes:
        challenged: The member the challenge names, or ``None`` when anyone may
            take it.
        accepter: Whoever took it, once somebody has.
        message: The message the buttons live on, set by the caller after
            sending so the timeout can redraw it.
        match: The board's view, once the challenge has been accepted.
    """

    def __init__(
        self,
        *,
        db: Database,
        rng: random.Random,
        challenger: discord.abc.User,
        challenged: discord.abc.User | None = None,
        bet: int,
        timezone: str,
        rake: float = 0.0,
        creator_tax_rate: float = 0.0,
        creator_tax_user_id: int | None = None,
        limiter: SlidingWindowLimiter | None = None,
    ) -> None:
        """Build a view offering a match.

        Args:
            db: Open database, used to take both stakes on acceptance.
            rng: Random source, used to decide who moves first.
            challenger: The member who called the match.
            challenged: The member being challenged, or ``None`` to leave the
                offer open to anyone.
            bet: Dollars each player stakes. The caller has already checked it
                against the table limit.
            timezone: IANA timezone for the embed timestamp.
            rake: Share of the house's cut to send to the lottery pot.
            creator_tax_rate: Share of the house's cut to send to
                ``creator_tax_user_id``.
            creator_tax_user_id: Bank account credited with the creator tax,
                or ``None`` to disable it outright.
            limiter: The shared action budget. Accepting is a wager that never
                passes through ``BaseCog.cog_check``, so it spends budget here
                instead; ``None`` disables that, which is what tests want.
        """
        super().__init__(timeout=connect4.CHALLENGE_TIMEOUT_SECONDS)
        self.db = db
        self.rng = rng
        self.challenger = challenger
        self.challenged = challenged
        self.bet = bet
        self.timezone = timezone
        self.rake = rake
        self.creator_tax_rate = creator_tax_rate
        self.creator_tax_user_id = creator_tax_user_id
        self.limiter = limiter
        self.message: discord.Message | None = None
        self.match: Connect4View | None = None
        self.accepter: discord.abc.User | None = None
        self.declined_by: discord.abc.User | None = None
        self._answered = False
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------- rendering --

    @property
    def is_open(self) -> bool:
        """Whether anyone may take the challenge, rather than one named member."""
        return self.challenged is None

    def _describe(self) -> str:
        """Name the challenge, for the messages that report how it ended."""
        if self.challenged is None:
            return f"{self.challenger.mention}'s open challenge for {embeds.money(self.bet)}"
        return f"{self.challenger.mention}'s challenge to {self.challenged.mention}"

    def embed(self) -> discord.Embed:
        """Return the embed for the challenge as it currently stands."""
        if self.declined_by is not None:
            who = "withdrawn" if self.declined_by.id == self.challenger.id else "declined"
            return embeds.error_embed(f"{self._describe()} was {who}. Nothing was staked.")
        if self._answered and self.match is None:
            return embeds.error_embed(f"{self._describe()} lapsed. Nothing was staked.")
        return embeds.connect4_challenge_embed(
            self.challenger, self.challenged, self.bet, self.timezone
        )

    # ------------------------------------------------------------- actions --

    def may_accept(self, accepter: discord.abc.User) -> str | None:
        """Return why a member cannot take this challenge, if they cannot.

        Args:
            accepter: The member trying to take it.

        Returns:
            ``None`` when they may, or a message explaining why not.
        """
        if accepter.id == self.challenger.id:
            return "You cannot accept your own challenge."
        challenged = self.challenged
        if challenged is not None and accepter.id != challenged.id:
            return f"That challenge is for {challenged.display_name}."
        return None

    async def apply_accept(self, accepter: discord.abc.User) -> str | None:
        """Take both stakes and start the match.

        Args:
            accepter: The member taking the challenge.

        Returns:
            ``None`` on success, or a message explaining why the match could
            not start. Neither stake is taken in that case.
        """
        problem = self.may_accept(accepter)
        if problem is not None:
            return problem

        async with self._lock:
            if self._answered:
                return "That challenge is no longer open."
            try:
                hold = await self.db.open_escrow(
                    "connect4", self.challenger.id, accepter.id, self.bet
                )
            except InsufficientFundsError:
                return await self._describe_shortfall(accepter)
            self._answered = True
            self.accepter = accepter

        first, second = self._seat(accepter)
        self.match = Connect4View(
            db=self.db,
            game=connect4.Game.new(),
            players=(first, second),
            hold_id=hold.hold_id,
            bet=self.bet,
            timezone=self.timezone,
            rake=self.rake,
            creator_tax_rate=self.creator_tax_rate,
            creator_tax_user_id=self.creator_tax_user_id,
        )
        self.stop()
        return None

    def apply_decline(self, user: discord.abc.User) -> str | None:
        """Close the challenge without staking anything.

        An open challenge is nobody's to turn down but the member who made it,
        so only they can withdraw one. A named challenge can be closed by
        either side.

        Args:
            user: Whoever is turning it down.

        Returns:
            ``None`` on success, or a message explaining the refusal.
        """
        if self._answered:
            return "That challenge is no longer open."
        if user.id != self.challenger.id and (
            self.challenged is None or user.id != self.challenged.id
        ):
            return "Only the member who opened that challenge can withdraw it."

        self._answered = True
        self.declined_by = user
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        return None

    def _seat(self, accepter: discord.abc.User) -> tuple[discord.abc.User, discord.abc.User]:
        """Decide who moves first.

        Moving first is a real advantage at Connect 4, so it is drawn rather
        than handed to whoever typed the command.
        """
        seated = [self.challenger, accepter]
        self.rng.shuffle(seated)
        return seated[0], seated[1]

    async def _describe_shortfall(self, accepter: discord.abc.User) -> str:
        """Name whichever player can no longer cover the stake."""
        for player in (self.challenger, accepter):
            account = await self.db.get_account(player.id)
            if account.wallet < self.bet:
                return (
                    f"{player.display_name} cannot cover {embeds.money(self.bet)} "
                    f"right now. Neither stake was taken."
                )
        return "One of you cannot cover the stake. Neither stake was taken."

    def _spend_budget(self, user_id: int) -> str | None:
        """Charge accepting against the shared action budget.

        Args:
            user_id: The member accepting.

        Returns:
            ``None`` when the press is allowed, or a message telling them how
            long to wait.
        """
        if self.limiter is None:
            return None
        wait = self.limiter.acquire(user_id)
        if wait:
            return f"You are acting too quickly. Try again in {max(1, round(wait))} seconds."
        return None

    # -------------------------------------------------------------- events --

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Let anyone press an open challenge, and only the two named answer one.

        An open challenge has to reach every member who might take it, so the
        rules about who may accept and who may withdraw are enforced in the
        callbacks instead.

        Args:
            interaction: The button press.

        Returns:
            Whether the press should be handled.
        """
        challenged = self.challenged
        if challenged is None or interaction.user.id in (self.challenger.id, challenged.id):
            return True
        await interaction.response.send_message(
            embed=embeds.error_embed("That challenge is not yours to answer."), ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        """Withdraw the challenge once it has gone unanswered for long enough.

        Nothing was staked, so nothing goes back; this only takes the live
        button off a stale offer.
        """
        if self._answered:
            return
        self._answered = True
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                log.warning("Could not redraw a lapsed Connect 4 challenge", exc_info=True)

    # ------------------------------------------------------------- buttons --

    @discord.ui.button(
        label="Accept", style=discord.ButtonStyle.success, custom_id="connect4:accept"
    )
    async def accept(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Connect4ChallengeView]
    ) -> None:
        """Take both stakes and put the board up in place of the challenge."""
        problem = self.may_accept(interaction.user)
        if problem is None:
            problem = self._spend_budget(interaction.user.id)
        if problem is None:
            problem = await self.apply_accept(interaction.user)
        if problem is not None:
            await interaction.response.send_message(
                embed=embeds.error_embed(problem), ephemeral=True
            )
            return

        assert self.match is not None  # noqa: S101 - set by a successful accept
        await interaction.response.edit_message(embed=self.match.embed(), view=self.match)
        self.match.message = self.message

    @discord.ui.button(
        label="Decline", style=discord.ButtonStyle.secondary, custom_id="connect4:decline"
    )
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Connect4ChallengeView]
    ) -> None:
        """Turn the challenge down, or withdraw it."""
        problem = self.apply_decline(interaction.user)
        if problem is not None:
            await interaction.response.send_message(
                embed=embeds.error_embed(problem), ephemeral=True
            )
            return
        await interaction.response.edit_message(embed=self.embed(), view=self)
