"""Interactive message components.

The button callbacks here do as little as possible: each one advances the hand
through an ``apply_*`` coroutine that takes no :class:`discord.Interaction`, then
redraws the message. Keeping the rules and the money out of the callbacks is
what makes the view testable without a gateway connection.
"""

from __future__ import annotations

import logging

import discord

from flyconomy import blackjack, embeds
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
    ) -> None:
        """Build a view for a freshly dealt hand.

        Args:
            db: Open database, used to credit the payout.
            game: The dealt hand.
            player: The member who owns the hand. Nobody else may press.
            base_bet: The opening stake, which is also what a double down costs.
            timezone: IANA timezone for the embed timestamp.
        """
        super().__init__(timeout=blackjack.DECISION_TIMEOUT_SECONDS)
        self.db = db
        self.game = game
        self.player = player
        self.base_bet = base_bet
        self.timezone = timezone
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
