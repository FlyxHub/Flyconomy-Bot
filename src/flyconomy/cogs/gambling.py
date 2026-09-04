"""Casino games.

Every game follows the same flow: the stake is debited from the wallet before
the outcome is rolled, and a win credits the stake multiplied by the game's
return. Debiting first means a member cannot bet money they do not have, even if
they fire two commands at once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import discord
from discord import app_commands
from discord.ext import commands

from flyconomy import blackjack, crash, economy, embeds, tictactoe
from flyconomy.bot import FlyconomyBot
from flyconomy.cogs.base import BaseCog
from flyconomy.errors import BetTooLargeError
from flyconomy.views import (
    BlackjackView,
    CrashView,
    JackpotView,
    MatchChallengeView,
    MatchView,
    TicTacToeView,
)

log = logging.getLogger(__name__)

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
        """Credit a win if there is one, and rake the house's take into the pot.

        Every game calls this exactly once per wager, including on a loss with a
        multiplier of zero, so the rake sees the full picture rather than only
        the losses.

        Args:
            ctx: Invocation context, used to identify the player.
            bet: The stake, already debited.
            multiplier: Stake multiplier to return. Zero for a loss.
        """
        if multiplier:
            await self.db.add_wallet(ctx.author.id, bet * multiplier)
        await self.rake(bet - bet * multiplier)

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
            await self._settle(ctx, bet, 0)
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
                await self._settle(ctx, bet, 0)
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
            await self._settle(ctx, bet, 0)
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
            await self._settle(ctx, bet, 0)
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
            rake=self.settings.lottery_rake,
            creator_tax_rate=self.settings.creator_tax_rate,
            creator_tax_user_id=self.settings.creator_tax_user_id,
        )

        # A natural for either side decides the hand on the deal, so there is
        # nothing to press and the buttons never appear.
        if game.finished:
            await view.settle()
            await ctx.send(embed=view.embed())
            return

        view.message = await ctx.send(embed=view.embed(), view=view)

    @commands.hybrid_command(name="crash")  # type: ignore[arg-type]
    @app_commands.describe(bet="Dollars to stake.")
    async def crash_command(
        self, ctx: commands.Context[FlyconomyBot], bet: commands.Range[int, 1]
    ) -> None:
        """Cash out before the multiplier crashes. The longer you wait, the more it pays."""
        await self._stake(ctx, bet)
        game = crash.Game.deal(bet, self.rng)

        view = CrashView(
            db=self.db,
            game=game,
            player=ctx.author,
            timezone=self.timezone,
            rake=self.settings.lottery_rake,
            creator_tax_rate=self.settings.creator_tax_rate,
            creator_tax_user_id=self.settings.creator_tax_user_id,
        )

        # An instant bust decides the round on the deal, so there is nothing
        # to press and the button never appears.
        if game.crash_point <= 1.0:
            await view.settle(multiplier=0.0)
            await ctx.send(embed=view.embed())
            return

        view.message = await ctx.send(embed=view.embed(), view=view)
        view.start_ticking()

    @commands.hybrid_command(name="jackpot", aliases=["jp"])  # type: ignore[arg-type]
    @app_commands.describe(ante="Dollars to ante into the pot.")
    async def jackpot_command(
        self, ctx: commands.Context[FlyconomyBot], ante: commands.Range[int, 1]
    ) -> None:
        """Ante into a shared pot. One entrant wins it all, and a bigger ante wins more often."""
        # Not routed through _stake: the ante and the entry have to move in one
        # transaction, so the debit happens inside enter_jackpot instead. The
        # table limit still has to be checked here, before anything is charged.
        self._check_limit(ante)

        async with self._jackpot_lock:
            live = self._live_jackpot
            if live is not None and not live.is_finished():
                await self._join_jackpot(ctx, live, ante)
                return
            await self._open_jackpot(ctx, ante)

    async def _join_jackpot(
        self, ctx: commands.Context[FlyconomyBot], live: JackpotView, ante: int
    ) -> None:
        """Ante into the round already running, and redraw it.

        Args:
            ctx: Invocation context, used to identify the player.
            live: The round in play.
            ante: Dollars to ante, already checked against the table limit.
        """
        problem = await live.apply_join(ctx.author.id, ante)
        if problem is not None:
            await ctx.send(problem)
            return

        await live.redraw()
        await ctx.send(
            f"You anted {embeds.money(ante)} into round #{live.state.round_number}. "
            f"The pot is {embeds.money(live.state.pot)} across "
            f"{live.state.entrants:,} entrants."
        )

    async def _open_jackpot(self, ctx: commands.Context[FlyconomyBot], ante: int) -> None:
        """Open a round on this member's ante and start its countdown.

        Args:
            ctx: Invocation context, used to identify the player.
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

        await self.db.enter_jackpot(ctx.author.id, ante)
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
        view.message = await ctx.send(embed=view.embed(), view=view)
        view.start_ticking()
        self._live_jackpot = view

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
                await self._settle(ctx, bet, 0)
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
            await self._settle(ctx, amount, 0)
            await ctx.send("Sorry, you lost your bet.")


async def setup(bot: FlyconomyBot) -> None:
    """Register the cog with the bot."""
    await bot.add_cog(Gambling(bot))
