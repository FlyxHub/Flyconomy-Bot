"""Interactive message components.

The button callbacks here do as little as possible: each one advances the hand
through an ``apply_*`` coroutine that takes no :class:`discord.Interaction`, then
redraws the message. Keeping the rules and the money out of the callbacks is
what makes the view testable without a gateway connection.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import discord
from discord.ext import tasks

from flyconomy import blackjack, crash, embeds, jackpot
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
