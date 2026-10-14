"""Personal ranking logic (Beli-style). Pure Python, no database calls,
so every piece can be unit tested on its own (see tests/test_ranking.py).

How a user ranks a new book:
  1. They pick a group: liked / fine / disliked.
  2. find_insert_index() runs a binary search over that group's list,
     asking "do you prefer the new book over this one?" each step.
     Placing a book in a list of n takes about log2(n) questions.
  3. The client sends the two neighbors (the book just above and the
     book just below the new one) to POST /reviews.
  4. plan_insert() checks those neighbors are still adjacent and picks a
     list_position between them.

Positions: LOWER = ranked higher. They're floats so we can always insert
between two books (between 1.0 and 2.0 -> 1.5) by writing a single row,
instead of renumbering everything below. Repeatedly inserting into the
same gap eventually runs out of float precision (about 50 halvings); when
that happens we renumber the whole group once ("rebalance").
"""
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")
GAP = 1.0  # spacing between positions when appending or rebalancing


class StaleListError(Exception):
    """The neighbors the client sent don't match the current list,
    e.g. the list changed in another tab since the client loaded it."""


def find_insert_index(ranked: Sequence[T], prefers_new_over: Callable[[T], bool]) -> int:
    """Binary search for where a new item belongs in a best-first list.

    ranked:            the existing list, best first.
    prefers_new_over:  asks the user "is the new book better than this one?"
    Returns the index the new item should take (0 = new #1).
    """
    lo, hi = 0, len(ranked)
    while lo < hi:
        mid = (lo + hi) // 2
        if prefers_new_over(ranked[mid]):
            hi = mid        # new book is better: it goes somewhere above mid
        else:
            lo = mid + 1    # new book is worse: it goes somewhere below mid
    return lo


def position_between(above: float | None, below: float | None) -> float | None:
    """A position strictly between two neighbors (None = no neighbor there).
    Returns None if no float fits between them (time to rebalance)."""
    if above is None and below is None:
        return GAP                  # first book in the group
    if above is None:
        return below - GAP          # new #1
    if below is None:
        return above + GAP          # new last place
    mid = (above + below) / 2
    return mid if above < mid < below else None


def plan_insert(
    group: list[tuple[str, float]],
    above_book_id: str | None,
    below_book_id: str | None,
) -> tuple[float, dict[str, float]]:
    """Decide where a new book goes in one of the user's groups.

    group:          [(book_id, list_position), ...] sorted best-first.
    above_book_id:  the book the user liked a bit MORE (None = new #1).
    below_book_id:  the book the user liked a bit LESS (None = new last).

    Returns (new_position, renumbered). `renumbered` is normally empty; after
    a rebalance it maps book_id -> new position for rows that must be updated.
    Raises StaleListError if the neighbors aren't adjacent in `group`.
    """
    ids = [book_id for book_id, _ in group]

    if above_book_id is None and below_book_id is None:
        if group:
            raise StaleListError(
                "This group already has books. Send above_book_id and/or "
                "below_book_id to say where the new one goes."
            )
        index = 0
    elif above_book_id is None:
        if not ids or ids[0] != below_book_id:
            raise StaleListError(f"{below_book_id} is not the #1 book in this group.")
        index = 0
    elif below_book_id is None:
        if not ids or ids[-1] != above_book_id:
            raise StaleListError(f"{above_book_id} is not the last book in this group.")
        index = len(ids)
    else:
        if (above_book_id not in ids or below_book_id not in ids
                or ids.index(below_book_id) != ids.index(above_book_id) + 1):
            raise StaleListError(
                f"{above_book_id} and {below_book_id} are not next to each other "
                "in this group. Reload the list and try again."
            )
        index = ids.index(below_book_id)

    above = group[index - 1][1] if index > 0 else None
    below = group[index][1] if index < len(group) else None

    position = position_between(above, below)
    if position is not None:
        return position, {}

    # Out of precision: renumber the group 1, 2, 3, ... leaving a slot at
    # `index` for the new book.
    renumbered = {
        book_id: (i + 2 if i >= index else i + 1) * GAP
        for i, (book_id, _) in enumerate(group)
    }
    return (index + 1) * GAP, renumbered
