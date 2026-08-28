"""Casino games.

Every game follows the same flow: the stake is debited from the wallet before
the outcome is rolled, and a win credits the stake multiplied by the game's
return. Debiting first means a member cannot bet money they do not have, even if
they fire two commands at once.
"""

from __future__ import annotations

from discord import app_commands
from discord.ext import commands

from flyconomy import blackjack, economy, embeds
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog
from flyconomy.errors import BetTooLargeError
from flyconomy.views import BlackjackView

_ROULETTE_HELP = (
    "Bet on `red`, `black`, or a single pocket (`0`, `00`, or `1`-`36`). "
    "A color pays 2x the stake and a single pocket pays 35x."
)


class Gambling(BaseCog, name="Casino"):
    """Wager wallet money on games of chance."""

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

    async def _stake(self, ctx: commands.Context[FlyconomyBot], bet: int) -> None:
        """Check the table limit, then debit the stake.

        Every game places its bet through here, so a new game cannot forget the
        limit.

        Args:
            ctx: Invocation context, used to identify the player.
            bet: Dollars to stake.

        Raises:
            BetTooLargeError: If the stake exceeds the table limit.
            InsufficientFundsError: If the wallet cannot cover the stake.
        """
        self._check_limit(bet)
        await self.db.add_wallet(ctx.author.id, -bet)

    async def _settle(self, ctx: commands.Context[FlyconomyBot], bet: int, multiplier: int) -> None:
        """Credit a win, if there is one.

        Args:
            ctx: Invocation context, used to identify the player.
            bet: The stake, already debited.
            multiplier: Stake multiplier to return. Zero for a loss.
        """
        if multiplier:
            await self.db.add_wallet(ctx.author.id, bet * multiplier)

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

        await self._stake(ctx, bet)
        flip = self.rng.choice(economy.COIN_SIDES)

        if choice == flip:
            await self._settle(ctx, bet, economy.COINFLIP_RETURN)
            await ctx.send(f"It's **{flip}**. You win **{embeds.money(bet * 2)}**")
        else:
            await ctx.send(f"It's **{flip}**, you lose **{embeds.money(bet)}**")

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

        await self._stake(ctx, bet)
        bot_move = self.rng.choice(economy.RPS_MOVES)
        result = economy.rps_outcome(move, bot_move)

        match result:
            case "tie":
                await self._settle(ctx, bet, economy.RPS_TIE_RETURN)
                await ctx.send(
                    f"The bot chose **{bot_move}**. It's a tie, and the house takes ties. "
                    f"You lose **{embeds.money(bet)}**"
                )
            case "win":
                await self._settle(ctx, bet, economy.RPS_RETURN)
                await ctx.send(
                    f"The bot chose **{bot_move}**. "
                    f"You win **{embeds.money(bet * economy.RPS_RETURN)}!**"
                )
            case _:
                await ctx.send(f"The bot chose **{bot_move}**. You lose **{embeds.money(bet)}**")

    @commands.hybrid_command(name="dice")  # type: ignore[arg-type]
    @app_commands.describe(guess="The face you expect, 1 to 6.", bet="Dollars to stake.")
    async def dice(
        self,
        ctx: commands.Context[FlyconomyBot],
        guess: commands.Range[int, 1, economy.DICE_SIDES],
        bet: commands.Range[int, 1],
    ) -> None:
        """Bet on a six-sided dice roll. A correct call returns 6x your stake."""
        await self._stake(ctx, bet)
        roll = self.rng.randint(1, economy.DICE_SIDES)

        if guess == roll:
            await self._settle(ctx, bet, economy.DICE_RETURN)
            await ctx.send(f"You rolled a **{roll}**. You win **{embeds.money(bet * 6)}**")
        else:
            await ctx.send(f"You rolled a **{roll}**. You lose **{embeds.money(bet)}**")

    @commands.hybrid_command(name="slots", aliases=["slot"])  # type: ignore[arg-type]
    @app_commands.describe(bet="Dollars to stake.")
    async def slots(self, ctx: commands.Context[FlyconomyBot], bet: commands.Range[int, 1]) -> None:
        """Spin the slot machine. Three of a kind returns up to 55x your stake."""
        await self._stake(ctx, bet)
        reels = economy.spin_slots(self.rng)
        multiplier = economy.slots_payout_multiplier(reels)

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

        await self._settle(ctx, bet, multiplier)
        await ctx.send(f"[ {window} ]\n{headline} You win **{embeds.money(bet * multiplier)}**")

    @commands.hybrid_command(name="blackjack", aliases=["bj"])  # type: ignore[arg-type]
    @app_commands.describe(bet="Dollars to stake.")
    async def blackjack_command(
        self, ctx: commands.Context[FlyconomyBot], bet: commands.Range[int, 1]
    ) -> None:
        """Play a hand of blackjack against the dealer. A natural pays 3:2."""
        await self._stake(ctx, bet)
        game = blackjack.Game.deal(bet, self.rng)

        view = BlackjackView(
            db=self.db,
            game=game,
            player=ctx.author,
            base_bet=bet,
            timezone=self.timezone,
        )

        # A natural for either side decides the hand on the deal, so there is
        # nothing to press and the buttons never appear.
        if game.finished:
            await view.settle()
            await ctx.send(embed=view.embed())
            return

        view.message = await ctx.send(embed=view.embed(), view=view)

    @commands.hybrid_command(name="war")  # type: ignore[arg-type]
    @app_commands.describe(bet="Dollars to stake.")
    async def war(self, ctx: commands.Context[FlyconomyBot], bet: commands.Range[int, 1]) -> None:
        """Draw a card against the dealer. The higher card wins, and a tie is returned."""
        await self._stake(ctx, bet)
        player, dealer = economy.draw_cards(2, self.rng)
        multiplier = economy.war_payout_multiplier(player, dealer)

        draw = f"You drew **{player}**, the dealer drew **{dealer}**."
        match multiplier:
            case economy.WAR_WIN_RETURN:
                await self._settle(ctx, bet, multiplier)
                await ctx.send(f"{draw} You win **{embeds.money(bet * multiplier)}**")
            case economy.WAR_TIE_RETURN:
                await self._settle(ctx, bet, multiplier)
                await ctx.send(f"{draw} It's a tie, so your {embeds.money(bet)} is returned.")
            case _:
                await ctx.send(f"{draw} You lose **{embeds.money(bet)}**")

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

        await self._stake(ctx, amount)
        pocket = self.rng.choice(economy.ROULETTE_WHEEL)
        multiplier = economy.roulette_payout_multiplier(wager, pocket)

        await ctx.send(f"And the roll is..... **{pocket}**")
        if multiplier:
            await self._settle(ctx, amount, multiplier)
            await ctx.send(
                f"Congratulations {ctx.author.mention}, "
                f"you won **{embeds.money(amount * multiplier)}**"
            )
        else:
            await ctx.send("Sorry, you lost your bet.")


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Gambling(bot))
