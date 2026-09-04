"""Connect 4 rules.

Like :mod:`flyconomy.blackjack` and :mod:`flyconomy.jackpot`, this module
imports nothing from ``discord``, so the whole ruleset is unit tested without a
gateway connection. It knows about a board and two disc colours; it knows
nothing about who is playing, what they staked, or how a match ends outside the
board -- a resignation and a timeout are match facts, not board facts, and live
with the view that owns the match.

This is the bot's first game of skill, and the first where the opposition is
another member rather than the house. That makes it zero-sum before the rake
and negative-sum after: the two stakes are all the money in play, and the
winner takes them less :data:`HOUSE_CUT`, so no result can pay out more than
was staked no matter how well anybody plays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Self

#: Board size. Five columns rather than the boxed game's seven, so the whole
#: board fits one row of buttons: Discord caps an action row at five, and a
#: seven-wide board wrapped two columns onto a second row that read as a
#: mistake. Everything below is written against these numbers rather than
#: against a literal seven, so the shape is the only thing that changed.
COLUMNS: Final = 5
ROWS: Final = 6

#: Discs in a row needed to win.
CONNECT: Final = 4

#: Cell values. Players are numbered rather than coloured so the rules never
#: need to know which member is which.
EMPTY: Final = 0
FIRST: Final = 1
SECOND: Final = 2

#: Share of the pot the house keeps from a decided match, matching the jackpot
#: so the two player-versus-player games price the same. A match between evenly
#: matched players therefore returns ``1 - HOUSE_CUT`` of each stake on average.
#: Nothing here depends on that number being right: the payout is bounded by
#: the pot whatever it is set to.
HOUSE_CUT: Final = 0.05

#: How long a challenge waits to be accepted before it lapses. Nothing is
#: staked until it is accepted, so a lapsed challenge costs nobody anything --
#: it only stops a stale offer sitting in the channel with a live button on it.
CHALLENGE_TIMEOUT_SECONDS: Final = 60

#: How long a player has to move. discord.py restarts a view's timeout on every
#: interaction, so this is per move rather than per match: a player who walks
#: away forfeits, which is what stops a stalled match from holding two stakes
#: in escrow forever.
MOVE_TIMEOUT_SECONDS: Final = 120


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

    The board is stored as a list of columns, each holding its discs from the
    bottom up, because that is exactly how a disc is dropped: the landing row
    is the height of the column it falls into. Reading a cell that has not been
    filled yet gives :data:`EMPTY`.

    Attributes:
        columns: One list of discs per column, bottom disc first.
        turn: The player to move, :data:`FIRST` or :data:`SECOND`.
        winner: The player who connected four, or ``None`` while nobody has.
        winning_cells: The ``(column, row)`` cells of the winning line, empty
            until there is one.
    """

    columns: list[list[int]] = field(default_factory=lambda: [[] for _ in range(COLUMNS)])
    turn: int = FIRST
    winner: int | None = None
    winning_cells: tuple[tuple[int, int], ...] = ()

    @classmethod
    def new(cls) -> Self:
        """Return an empty board with the first player to move."""
        return cls()

    # ------------------------------------------------------------- reading --

    def cell(self, column: int, row: int) -> int:
        """Return what occupies a cell.

        Args:
            column: Column index, from zero on the left.
            row: Row index, from zero at the bottom.

        Returns:
            :data:`FIRST`, :data:`SECOND`, or :data:`EMPTY`.
        """
        if not (0 <= column < COLUMNS and 0 <= row < ROWS):
            return EMPTY
        stack = self.columns[column]
        return stack[row] if row < len(stack) else EMPTY

    def height(self, column: int) -> int:
        """Return how many discs a column already holds."""
        return len(self.columns[column])

    def can_drop(self, column: int) -> bool:
        """Return whether a disc can still be dropped into a column."""
        if self.finished or not (0 <= column < COLUMNS):
            return False
        return self.height(column) < ROWS

    @property
    def moves(self) -> int:
        """How many discs have been played."""
        return sum(len(stack) for stack in self.columns)

    @property
    def is_full(self) -> bool:
        """Whether every cell is occupied."""
        return self.moves >= COLUMNS * ROWS

    @property
    def finished(self) -> bool:
        """Whether the board itself has decided the match."""
        return self.winner is not None or self.is_full

    @property
    def is_draw(self) -> bool:
        """Whether the board filled up with nobody connecting four."""
        return self.winner is None and self.is_full

    @property
    def open_columns(self) -> tuple[int, ...]:
        """The columns a disc can still be dropped into."""
        return tuple(column for column in range(COLUMNS) if self.can_drop(column))

    # ------------------------------------------------------------- playing --

    def drop(self, column: int) -> int:
        """Drop the moving player's disc into a column.

        Settles the board if the disc connects four, and otherwise passes the
        turn. A move that ends the match does not pass the turn, so the loser
        is always :func:`other_player` of the winner.

        Args:
            column: Column index, from zero on the left.

        Returns:
            The row the disc landed in, from zero at the bottom.

        Raises:
            ValueError: If the column is out of range, already full, or the
                match is already decided.
        """
        if self.finished:
            msg = "the match is already decided"
            raise ValueError(msg)
        if not 0 <= column < COLUMNS:
            msg = f"column {column} is not on the board"
            raise ValueError(msg)
        if self.height(column) >= ROWS:
            msg = f"column {column} is full"
            raise ValueError(msg)

        player = self.turn
        row = self.height(column)
        self.columns[column].append(player)

        line = self._line_through(column, row, player)
        if line is not None:
            self.winner = player
            self.winning_cells = line
        elif not self.is_full:
            self.turn = other_player(player)
        return row

    def _line_through(
        self, column: int, row: int, player: int
    ) -> tuple[tuple[int, int], ...] | None:
        """Return the winning line through a just-played disc, if there is one.

        Only the four directions through the new disc can have completed a
        line, so the whole board never needs scanning.

        Args:
            column: Column the disc landed in.
            row: Row the disc landed in.
            player: The disc's owner.

        Returns:
            The cells of the first line of at least :data:`CONNECT` discs
            found, ordered, or ``None`` if the disc completed none.
        """
        for step_column, step_row in ((1, 0), (0, 1), (1, 1), (1, -1)):
            cells = [(column, row)]
            for direction in (1, -1):
                next_column = column + step_column * direction
                next_row = row + step_row * direction
                while self.cell(next_column, next_row) == player:
                    cells.append((next_column, next_row))
                    next_column += step_column * direction
                    next_row += step_row * direction
            if len(cells) >= CONNECT:
                return tuple(sorted(cells))
        return None
