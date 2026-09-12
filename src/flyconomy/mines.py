"""Mines rules.

Like :mod:`flyconomy.crash`, this module imports nothing from ``discord``, so
the whole ruleset is unit tested without a gateway connection. A round hides
:attr:`Game.mine_count` mines among :data:`TILES` tiles at deal time; the
player turns tiles over one at a time, and every safe tile raises the
multiplier they may cash out at. One mine ends the round and takes the stake.

House edge is a single constant, :data:`HOUSE_EDGE`, and like crash -- and
unlike blackjack -- it is flat across stopping rules rather than tied to one:
turning over exactly ``k`` tiles and cashing out has an expected profit of
``-HOUSE_EDGE * stake`` for every ``k`` the multiplier cap does not bind, and
a worse one for the one depth where it does. See :func:`multiplier_for` for
the derivation. So there is no clever depth to stop at: a player can choose
how much variance to take, never how much edge to give up, and the only way
the choice matters at all is that pressing past the cap is worse.

The board is four rows of four because that is what Discord's component budget
allows, not because sixteen is a nice number. Twenty-five buttons is the whole
budget of five rows of five, which would leave nowhere to put Cash Out; sixteen
tiles take four rows and leave the fifth for it. See the note on action rows in
``CLAUDE.md`` -- a board is designed to that limit rather than against it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from math import comb
from typing import Final, Self

#: Rows and columns of tiles. Four of each, for the component-budget reason in
#: the module docstring.
ROWS: Final = 4
COLUMNS: Final = 4

#: Tiles on a board.
TILES: Final = ROWS * COLUMNS

#: Share of every wager the house keeps, on average, whatever the player's
#: stopping rule. See :func:`multiplier_for` for why this holds for every
#: number of tiles turned over, not just one.
HOUSE_EDGE: Final = 0.03

#: How many mines a member may ask for, and what they get if they don't ask.
#: The floor of one keeps at least one tile dangerous; the ceiling of eight
#: keeps at least half the board safe, so the opening press is never worse
#: than a coin toss.
MIN_MINES: Final = 1
MAX_MINES: Final = 8
DEFAULT_MINES: Final = 3

#: Hard cap on the multiplier a round can reach, so a cleared board cannot pay
#: an unbounded multiple of the table limit. Uncapped, eight mines fully
#: cleared pays 12,484x, which at the default table limit is $1.2B from one
#: press -- a fifth of the way to the season's whole supply ceiling. Like
#: crash's cap this can only ever push the realized edge above
#: :data:`HOUSE_EDGE`, never below it: see :func:`multiplier_for`.
MAX_MULTIPLIER: Final = 100.0

#: What a round pays when the player walks away without turning over a single
#: tile: exactly the stake back. A round nobody played is a void rather than a
#: result, the same way an unplayable head-to-head match refunds both sides.
#: It cannot be farmed, because breaking even is the best it can do.
REFUND_MULTIPLIER: Final = 1.0

#: How long a member has to press before the round settles itself. Unlike
#: crash there is no clock in the game, so this is the round's real end rather
#: than a safety net: a walked-away round cashes out at whatever it had
#: reached, because the guide promises nothing is ever stranded.
DECISION_TIMEOUT_SECONDS: Final = 180

#: Fallback random source. Game outcomes, so a seedable PRNG is correct here.
_DEFAULT_RNG: Final = random.Random()  # noqa: S311

#: What one tile is showing. Named here rather than in :mod:`flyconomy.embeds`
#: so deciding it stays testable without a gateway; how each one *looks* is the
#: embed module's business, the same split as tic-tac-toe's marks.
HIDDEN: Final = "hidden"
SAFE: Final = "safe"
BUSTED: Final = "busted"
MINE: Final = "mine"


def safe_tiles(mine_count: int) -> int:
    """Return how many tiles on a board of ``mine_count`` mines are safe."""
    return TILES - mine_count


def survival_probability(mine_count: int, revealed: int) -> float:
    """Return the chance a fresh board survives ``revealed`` presses.

    The mines are placed uniformly, so which tiles the player picks does not
    matter -- only how many. That makes this the hypergeometric
    ``C(safe, revealed) / C(TILES, revealed)``.

    Args:
        mine_count: Mines hidden on the board.
        revealed: Tiles turned over, all of them safe.

    Returns:
        The probability, in ``[0, 1]``.
    """
    return comb(safe_tiles(mine_count), revealed) / comb(TILES, revealed)


def multiplier_for(mine_count: int, revealed: int) -> float:
    """Return the multiplier a player may cash out at after ``revealed`` tiles.

    The fair multiplier is ``1 / survival_probability``, which makes a strategy
    of "turn over exactly ``k`` tiles, then stop" pay
    ``survival_probability(m, k) * multiplier_for(m, k) = 1 - HOUSE_EDGE`` of
    the stake, whatever ``k`` is. Every stopping rule therefore meets the same
    edge, so there is no depth worth finding and no reason to prefer one mine
    count over another.

    Clamping to :data:`MAX_MULTIPLIER` only ever pays a deep board *less* than
    fair, which can raise the realized edge above :data:`HOUSE_EDGE` but can
    never push it negative.

    Args:
        mine_count: Mines hidden on the board.
        revealed: Safe tiles turned over so far.

    Returns:
        The multiplier, or ``0.0`` when nothing has been turned over yet --
        a round with no presses in it has nothing to cash out.
    """
    if revealed <= 0:
        return 0.0
    fair = (1 - HOUSE_EDGE) / survival_probability(mine_count, revealed)
    return min(fair, MAX_MULTIPLIER)


def payout(stake: int, multiplier: float) -> int:
    """Return the dollars credited back for cashing out at ``multiplier``.

    Args:
        stake: Everything staked on the round.
        multiplier: The multiplier cashed out at, or ``0.0`` for a bust.

    Returns:
        Dollars to credit. Integer division rounds in the house's favor, the
        same as :func:`flyconomy.crash.payout`.
    """
    return int(stake * multiplier)


@dataclass(slots=True)
class Game:
    """One round of mines.

    Attributes:
        stake: Dollars staked, already debited by the caller.
        mine_tiles: Where the mines are, drawn once at deal time and not
            revealed until the round ends.
        revealed: Safe tiles turned over so far, in press order.
        busted_tile: The mine that ended the round, or ``None`` while it is
            still live or if it ended in a cash-out.
    """

    stake: int
    mine_tiles: frozenset[int]
    revealed: list[int] = field(default_factory=list)
    busted_tile: int | None = None

    @classmethod
    def deal(cls, stake: int, mine_count: int, rng: random.Random | None = None) -> Self:
        """Hide ``mine_count`` mines on a fresh board.

        Args:
            stake: Dollars staked, already debited by the caller.
            mine_count: Mines to hide, between :data:`MIN_MINES` and
                :data:`MAX_MINES`.
            rng: Random source, injectable for deterministic tests.

        Returns:
            The dealt round, with its mines placed but nothing turned over.

        Raises:
            ValueError: If ``mine_count`` is outside the playable range.
        """
        if not MIN_MINES <= mine_count <= MAX_MINES:
            message = f"mine_count must be between {MIN_MINES} and {MAX_MINES}, got {mine_count}"
            raise ValueError(message)
        source = rng if rng is not None else _DEFAULT_RNG
        return cls(stake=stake, mine_tiles=frozenset(source.sample(range(TILES), mine_count)))

    @property
    def mine_count(self) -> int:
        """How many mines are hidden on the board."""
        return len(self.mine_tiles)

    @property
    def multiplier(self) -> float:
        """The multiplier the player may cash out at right now."""
        return multiplier_for(self.mine_count, len(self.revealed))

    @property
    def next_multiplier(self) -> float:
        """What one more safe tile would be worth, for the live embed."""
        return multiplier_for(self.mine_count, len(self.revealed) + 1)

    @property
    def busted(self) -> bool:
        """Whether the round ended on a mine."""
        return self.busted_tile is not None

    @property
    def cleared(self) -> bool:
        """Whether every safe tile has been turned over."""
        return len(self.revealed) >= safe_tiles(self.mine_count)

    @property
    def at_ceiling(self) -> bool:
        """Whether the multiplier has been clamped to :data:`MAX_MULTIPLIER`.

        Pressing on from here risks the stake for nothing, since the clamp
        means the next safe tile pays no more than this one did. The view
        cashes out on the player's behalf rather than leaving a strictly
        losing button live.
        """
        return self.multiplier >= MAX_MULTIPLIER

    @property
    def exhausted(self) -> bool:
        """Whether the round has nothing left worth pressing."""
        return self.cleared or self.at_ceiling

    def is_mine(self, tile: int) -> bool:
        """Return whether ``tile`` hides a mine."""
        return tile in self.mine_tiles

    def can_reveal(self, tile: int) -> bool:
        """Return whether ``tile`` is one this round can still turn over."""
        return 0 <= tile < TILES and tile not in self.revealed and not self.busted

    def reveal(self, tile: int) -> bool:
        """Turn ``tile`` over, ending the round if it hides a mine.

        Args:
            tile: The tile pressed, counting left to right and top to bottom.

        Returns:
            Whether the tile was safe.
        """
        if self.is_mine(tile):
            self.busted_tile = tile
            return False
        self.revealed.append(tile)
        return True


def tile_state(game: Game, tile: int, *, finished: bool) -> str:
    """Return what ``tile`` should be showing.

    The mines stay hidden while the round is live, so a board the player is
    still pressing never gives away where the danger is. A finished round
    turns them all face up, win or lose, so the board that was played is
    left on screen to read.

    Args:
        game: The round.
        tile: The tile, counting left to right and top to bottom.
        finished: Whether the round has been settled.

    Returns:
        One of :data:`HIDDEN`, :data:`SAFE`, :data:`BUSTED`, or :data:`MINE`.
    """
    if tile == game.busted_tile:
        return BUSTED
    if tile in game.revealed:
        return SAFE
    if finished and game.is_mine(tile):
        return MINE
    return HIDDEN


def abandoned_multiplier(game: Game) -> float:
    """Return what ``game`` pays if the player walks away from it.

    A round with tiles turned over cashes out at what it had reached, and an
    untouched one is refunded at :data:`REFUND_MULTIPLIER`. Both are stopping
    rules the edge already covers, so neither is worth waiting for.
    """
    if not game.revealed:
        return REFUND_MULTIPLIER
    return game.multiplier
