"""Unit tests for the ranking algorithm. Run with:  pytest"""
import math
import random

import pytest

from ranking import (
    GAP,
    StaleListError,
    find_insert_index,
    plan_insert,
    position_between,
)


# ---------- find_insert_index (binary search) ----------

def simulate(ranked: list[int], new: int) -> tuple[int, int]:
    """Pretend a user whose 'true' preference is: higher number = better.
    Returns (index chosen, number of questions asked)."""
    questions = 0

    def prefers_new_over(existing):
        nonlocal questions
        questions += 1
        return new > existing

    return find_insert_index(ranked, prefers_new_over), questions


def test_empty_list_needs_no_questions():
    assert simulate([], 5) == (0, 0)


def test_new_best_goes_to_top():
    assert simulate([9, 7, 5], 10)[0] == 0


def test_new_worst_goes_to_bottom():
    assert simulate([9, 7, 5], 1)[0] == 3


def test_new_goes_in_the_middle():
    assert simulate([9, 7, 5, 3], 6)[0] == 2


def test_builds_correct_order_and_uses_log_n_questions():
    random.seed(0)
    true_scores = random.sample(range(1000), 200)
    ranked: list[int] = []
    for score in true_scores:
        index, questions = simulate(ranked, score)
        # binary search: at most ceil(log2(n + 1)) questions
        assert questions <= math.ceil(math.log2(len(ranked) + 1))
        ranked.insert(index, score)
    assert ranked == sorted(true_scores, reverse=True)


# ---------- position_between ----------

def test_position_between_cases():
    assert position_between(None, None) == GAP
    assert position_between(None, 3.0) == 3.0 - GAP
    assert position_between(3.0, None) == 3.0 + GAP
    assert position_between(1.0, 2.0) == 1.5


def test_position_between_detects_exhausted_precision():
    a = 1.0
    b = math.nextafter(a, 2.0)  # the very next float after 1.0
    assert position_between(a, b) is None


# ---------- plan_insert ----------

GROUP = [("dune", 1.0), ("circe", 2.0), ("emma", 3.0)]


def test_first_book_in_empty_group():
    assert plan_insert([], None, None) == (GAP, {})


def test_insert_at_top_middle_bottom():
    assert plan_insert(GROUP, None, "dune") == (0.0, {})
    assert plan_insert(GROUP, "dune", "circe") == (1.5, {})
    assert plan_insert(GROUP, "emma", None) == (4.0, {})


@pytest.mark.parametrize("above,below", [
    (None, None),          # group isn't empty
    (None, "circe"),       # circe isn't #1
    ("circe", None),       # circe isn't last
    ("dune", "emma"),      # not adjacent
    ("emma", "dune"),      # wrong order
    ("dune", "dune"),      # same book twice
    ("dune", "missing"),   # not in the list
])
def test_rejects_stale_or_invalid_neighbors(above, below):
    with pytest.raises(StaleListError):
        plan_insert(GROUP, above, below)


def test_repeated_inserts_into_one_gap_eventually_rebalance():
    group = [("a", 1.0), ("z", 2.0)]
    rebalanced = False
    for i in range(200):
        # always insert right below "a", squeezing the same gap
        new_id = f"b{i}"
        position, renumbered = plan_insert(group, "a", group[1][0])
        if renumbered:
            rebalanced = True
            group = sorted(((bid, renumbered[bid]) for bid, _ in group), key=lambda r: r[1])
        group.insert(1, (new_id, position))
        positions = [p for _, p in group]
        assert positions == sorted(positions)
        assert len(set(positions)) == len(positions)   # no ties
    assert rebalanced
    assert group[0][0] == "a" and group[-1][0] == "z"
