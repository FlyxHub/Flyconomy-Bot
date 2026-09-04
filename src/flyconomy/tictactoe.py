"""Tic-tac-toe rules.

Like :mod:`flyconomy.connect4`, this module imports nothing from ``discord``,
so the whole ruleset is unit tested without a gateway connection. It knows
about one board; a match is several boards, and that lives with the view.

**A match is best-of-three boards, not one.** Tic-tac-toe between two people
paying attention is a draw every time, and a single drawn board would refund
both stakes and leave the wager pointless. Replaying a drawn board -- with the
seats swapped, since moving first is the whole of the advantage here -- means
the money follows the first mistake either player makes. A match where nobody
makes one inside :data:`BOARDS_PER_MATCH` boards is called off and both stakes
go back, which is the honest outcome for two players who never slipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Self

#: Board size. Nine cells, which is also nine buttons: three rows of three, so
#: the grid the players press *is* the board rather than a picture of one.
SIZE: Final = 3
CELLS: Final = SIZE * SIZE

#: Cell values. Players are numbered rather than marked so the rules never need
#: to know which member is X.
EMPTY: Final = 0
FIRST: Final = 1
SECOND: Final = 2

#: Every line that wins: three rows, three columns, two diagonals.
LINES: Final[tuple[tuple[int, int, int], ...]] = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)

#: Drawn boards replayed before a match is called off and both stakes returned.
#: Odd, so the seat advantage is shared as evenly as it can be: with the seats
#: swapping each board, three boards give one player two first moves and the
#: other one, rather than the whole advantage going one way.
BOARDS_PER_MATCH: Final = 3

#: Share of the pot the house keeps from a decided match, matching the other
#: player-versus-player games. Nothing here depends on that number being right:
#: the payout is bounded by the pot whatever it is set to.
HOUSE_CUT: Final = 0.05

#: How long a challenge waits to be accepted before it lapses. Nothing is
#: staked until it is accepted, so a lapsed challenge costs nobody anything.
CHALLENGE_TIMEOUT_SECONDS: Final = 60

#: How long a player has to move. discord.py restarts a view's timeout on every
#: interaction, so this is per move rather than per match: a player who walks
#: away forfeits, which is what stops a stalled match from holding two stakes
#: in escrow forever. Shorter than Connect 4's, because a move here is a glance
#: at nine cells rather than a plan.
MOVE_TIMEOUT_SECONDS: Final = 90


def other_player(player: int) -> int:
    """Return the player whose turn it is not.

    Args:
        player: :data:`FIRST` or :data:`SECOND`.

    Returns:
        The other one.
    """
    return SECOND if player == FIRST else FIRST


def house_cut(pot: int) -> int:
    """Return the dollars the house keeps from a decided match.

    Args:
        pot: Both stakes together.

    Returns:
        The house's cut, truncated down, which favours the player.
    """
    return int(pot * HOUSE_CUT)


def payout(pot: int) -> int:
    """Return the dollars the winner takes from a decided match.

    Args:
        pot: Both stakes together.

    Returns:
        The pot less :func:`house_cut`, which is always less than the pot.
    """
    return pot - house_cut(pot)


@dataclass(slots=True)
class Game:
    """One board.

    Cells are numbered left to right, top to bottom, which is the order the
    buttons are laid out in, so a button's index is its cell with nothing in
    between to get wrong.

    Attributes:
        cells: The nine cells, each :data:`EMPTY`, :data:`FIRST`, or
            :data:`SECOND`.
        turn: The player to move.
        winner: The player who made a line, or ``None`` while nobody has.
        winning_cells: The cells of the winning line, empty until there is one.
    """

    cells: list[int] = field(default_factory=lambda: [EMPTY] * CELLS)
    turn: int = FIRST
    winner: int | None = None
    winning_cells: tuple[int, ...] = ()

    @classmethod
    def new(cls) -> Self:
        """Return an empty board with the first player to move."""
        return cls()

    # ------------------------------------------------------------- reading --

    def cell(self, index: int) -> int:
        """Return what occupies a cell, or :data:`EMPTY` if it is off the board."""
        if not 0 <= index < CELLS:
            return EMPTY
        return self.cells[index]

    def can_place(self, index: int) -> bool:
        """Return whether a mark can still go in a cell."""
        if self.finished or not 0 <= index < CELLS:
            return False
        return self.cells[index] == EMPTY

    @property
    def moves(self) -> int:
        """How many marks have been played."""
        return sum(1 for cell in self.cells if cell != EMPTY)

    @property
    def is_full(self) -> bool:
        """Whether every cell is taken."""
        return self.moves >= CELLS

    @property
    def finished(self) -> bool:
        """Whether the board has been decided, by a line or by filling up."""
        return self.winner is not None or self.is_full

    @property
    def is_draw(self) -> bool:
        """Whether the board filled up with nobody making a line."""
        return self.winner is None and self.is_full

    @property
    def open_cells(self) -> tuple[int, ...]:
        """The cells still free to play."""
        return tuple(index for index in range(CELLS) if self.can_place(index))

    # ------------------------------------------------------------- playing --

    def place(self, index: int) -> None:
        """Put the moving player's mark in a cell.

        Settles the board if the mark makes a line, and otherwise passes the
        turn. A move that ends the board does not pass the turn, so the loser
        is always :func:`other_player` of the winner.

        Args:
            index: Cell to play, counting left to right and top to bottom.

        Raises:
            ValueError: If the cell is off the board, already taken, or the
                board is already decided.
        """
        if self.finished:
            msg = "the board is already decided"
            raise ValueError(msg)
        if not 0 <= index < CELLS:
            msg = f"cell {index} is not on the board"
            raise ValueError(msg)
        if self.cells[index] != EMPTY:
            msg = f"cell {index} is already taken"
            raise ValueError(msg)

        player = self.turn
        self.cells[index] = player

        line = self._line_through(index, player)
        if line is not None:
            self.winner = player
            self.winning_cells = line
        elif not self.is_full:
            self.turn = other_player(player)

    def _line_through(self, index: int, player: int) -> tuple[int, ...] | None:
        """Return the winning line through a just-played mark, if there is one.

        Only lines containing the new mark can have been completed by it, so
        the other lines are never looked at.

        Args:
            index: The cell just played.
            player: The mark's owner.

        Returns:
            The cells of the completed line, or ``None`` if it completed none.
        """
        for line in LINES:
            if index in line and all(self.cells[cell] == player for cell in line):
                return line
        return None
