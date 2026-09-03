"""Interactive message components.

The button callbacks here do as little as possible: each one advances the hand
through an ``apply_*`` coroutine that takes no :class:`discord.Interaction`, then
redraws the message. Keeping the rules and the money out of the callbacks is
what makes the view testable without a gateway connection.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import discord
from discord.ext import tasks

from flyconomy import blackjack, crash, embeds
from flyconomy.database import Database
from flyconomy.errors import InsufficientFundsError

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
