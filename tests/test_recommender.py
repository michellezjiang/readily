"""Unit tests for the recommender. No network: the LLM reply is a string
and Google Books is replaced by a fake search function."""
import pytest

from recommender import (
    RecommenderError,
    build_prompt,
    normalize_title,
    parse_suggestions,
    verify,
)

# A tiny fake "Google Books"
CATALOG = [
    {"id": "g-piranesi", "title": "Piranesi", "authors": ["Susanna Clarke"]},
    {"id": "g-wind", "title": "The Name of the Wind", "authors": ["Patrick Rothfuss"]},
    {"id": "g-dune", "title": "Dune", "authors": ["Frank Herbert"]},
]


def fake_search(query: str, limit: int) -> list[dict]:
    q = query.lower()
    return [b for b in CATALOG if normalize_title(b["title"]) in q][:limit]


def suggestion(title, author="", reason="because"):
    return {"title": title, "author": author, "reason": reason}


# ---------- prompt ----------

def test_prompt_includes_liked_and_disliked_books():
    liked = [{"title": "Dune", "authors": ["Frank Herbert"], "score": 10.0}]
    disliked = [{"title": "Twilight", "authors": ["Stephenie Meyer"], "score": 3.3}]
    prompt = build_prompt(liked, disliked, 5)
    assert '"Dune" by Frank Herbert (their score: 10.0/10)' in prompt
    assert "DISLIKED" in prompt and "Twilight" in prompt
    assert "Recommend 5" in prompt


def test_prompt_skips_disliked_section_when_empty():
    prompt = build_prompt([{"title": "Dune", "authors": [], "score": 10}], [], 3)
    assert "DISLIKED" not in prompt


# ---------- parsing ----------

def test_parses_valid_json():
    text = '{"recommendations": [{"title": "Piranesi", "author": "Susanna Clarke", "reason": "x"}]}'
    assert parse_suggestions(text) == [suggestion("Piranesi", "Susanna Clarke", "x")]


def test_parses_json_inside_code_fences():
    text = '```json\n{"recommendations": [{"title": "Piranesi"}]}\n```'
    assert parse_suggestions(text)[0]["title"] == "Piranesi"


def test_skips_malformed_entries():
    text = '{"recommendations": [{"title": ""}, "oops", {"author": "no title"}, {"title": "Dune"}]}'
    assert [s["title"] for s in parse_suggestions(text)] == ["Dune"]


@pytest.mark.parametrize("text", ["not json at all", '{"recommendations": "nope"}'])
def test_rejects_unusable_replies(text):
    with pytest.raises(RecommenderError):
        parse_suggestions(text)


# ---------- title matching ----------

@pytest.mark.parametrize("a,b", [
    ("The Name of the Wind", "Name of the Wind"),
    ("The Name of the Wind: The Kingkiller Chronicle", "name of the wind"),
    ("Piranesi!", "piranesi"),
])
def test_normalize_title_treats_variants_as_equal(a, b):
    assert normalize_title(a) == normalize_title(b)


# ---------- verification ----------

def test_keeps_real_books_and_attaches_reason():
    verified, dropped = verify([suggestion("Piranesi", "Susanna Clarke", "dreamy")],
                               set(), set(), fake_search, 5)
    assert verified == [{**CATALOG[0], "reason": "dreamy"}]
    assert dropped == []


def test_drops_hallucinated_book():
    verified, dropped = verify([suggestion("The Clockwork Librarian", "Jane Doe")],
                               set(), set(), fake_search, 5)
    assert verified == []
    assert dropped[0]["why"] == "not found in Google Books"


def test_drops_real_title_with_wrong_author():
    verified, dropped = verify([suggestion("Piranesi", "Stephen King")],
                               set(), set(), fake_search, 5)
    assert verified == [] and dropped[0]["why"] == "not found in Google Books"


def test_drops_books_already_ranked_by_id_or_title():
    verified, dropped = verify(
        [suggestion("Dune", "Frank Herbert"), suggestion("Name of the Wind", "Patrick Rothfuss")],
        already_ranked_ids={"g-dune"},
        already_ranked_titles={"The Name of the Wind"},
        search=fake_search, count=5,
    )
    assert verified == []
    assert len(dropped) == 2


def test_drops_duplicates_and_stops_at_count():
    suggestions = [suggestion("Piranesi"), suggestion("Piranesi"), suggestion("Dune"), suggestion("The Name of the Wind")]
    verified, dropped = verify(suggestions, set(), set(), fake_search, 2)
    assert [b["id"] for b in verified] == ["g-piranesi", "g-dune"]
    assert dropped == [{**suggestion("Piranesi"), "why": "already ranked or duplicate"}]
