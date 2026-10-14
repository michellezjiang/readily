"""AI book recommendations.

Flow:
  1. build_prompt()      turn the user's rankings into a prompt
  2. ask_llm()           send it to OpenAI, get JSON back
  3. parse_suggestions() turn the reply into [{title, author, reason}]
  4. verify()            check each suggestion against Google Books and
                         drop ones that don't exist (LLMs invent titles)
                         or that the user has already ranked

Everything except ask_llm() is plain Python with no network calls, so it
can be unit tested (see tests/test_recommender.py). verify() takes the
search function as a parameter for the same reason: tests pass a fake one.
"""
import json
import os
import re
from typing import Callable

from dotenv import load_dotenv

load_dotenv()

MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")


class RecommenderError(Exception):
    """The LLM call failed or returned something unusable."""


# ---------- 1. Prompt ----------

def build_prompt(liked: list[dict], disliked: list[dict], count: int) -> str:
    """liked/disliked: rows from the user's ranked list (best first)."""
    def describe(book):
        authors = ", ".join(book.get("authors") or []) or "unknown author"
        return f'- "{book["title"]}" by {authors} (their score: {book["score"]}/10)'

    lines = [
        "You recommend books. Here is how a reader ranked books they've read.",
        "",
        "Books they LIKED, best first:",
        *[describe(b) for b in liked],
    ]
    if disliked:
        lines += ["", "Books they DISLIKED (avoid books like these):", *[describe(b) for b in disliked]]
    lines += [
        "",
        f"Recommend {count} real, published books they have NOT read.",
        "Do not include any book listed above.",
        "For each, give a one-sentence reason that refers to specific books they liked.",
        'Respond with JSON only, in this exact shape: '
        '{"recommendations": [{"title": "...", "author": "...", "reason": "..."}]}',
    ]
    return "\n".join(lines)


# ---------- 2. LLM call ----------

def ask_llm(prompt: str) -> str:
    from openai import OpenAI, OpenAIError

    if not os.environ.get("OPENAI_API_KEY"):
        raise RecommenderError("OPENAI_API_KEY is missing from .env")
    try:
        response = OpenAI(timeout=60).chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},  # ask for valid JSON
        )
    except OpenAIError as e:
        raise RecommenderError(f"OpenAI request failed: {e}") from e
    return response.choices[0].message.content or ""


# ---------- 3. Parse ----------

def parse_suggestions(text: str) -> list[dict]:
    """Pull [{title, author, reason}] out of the model's reply.
    Tolerates code fences and skips malformed entries instead of crashing."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RecommenderError(f"Model didn't return valid JSON: {text[:200]}") from e

    items = data.get("recommendations", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RecommenderError("Model's JSON had no 'recommendations' list")

    suggestions = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("title"), str) and item["title"].strip():
            suggestions.append({
                "title": item["title"].strip(),
                "author": str(item.get("author") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            })
    return suggestions


# ---------- 4. Verify ----------

def normalize_title(title: str) -> str:
    """'The Name of the Wind: Kingkiller #1' -> 'name of the wind'"""
    t = title.lower().split(":")[0]
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"^(the|a|an) ", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def _author_matches(suggested: str, actual: list[str]) -> bool:
    if not suggested:
        return True  # no author given, nothing to contradict
    last_name = re.sub(r"[^a-z]", "", suggested.lower().split()[-1])
    return any(last_name in a.lower() for a in actual)


def verify(
    suggestions: list[dict],
    already_ranked_ids: set[str],
    already_ranked_titles: set[str],
    search: Callable[[str, int], list[dict]],
    count: int,
) -> tuple[list[dict], list[dict]]:
    """Check each suggestion is a real book the user hasn't ranked.

    search: a function like books_api.search_books(query, limit).
    Returns (verified, filtered_out). filtered_out explains every drop,
    which is handy for debugging and for showing how often the model is wrong.
    """
    seen_titles = {normalize_title(t) for t in already_ranked_titles}
    verified, filtered_out = [], []

    for s in suggestions:
        if len(verified) == count:
            break
        wanted = normalize_title(s["title"])

        if wanted in seen_titles:
            filtered_out.append({**s, "why": "already ranked or duplicate"})
            continue

        query = f'intitle:"{s["title"]}"' + (f' inauthor:"{s["author"]}"' if s["author"] else "")
        results = search(query, 5)
        match = next(
            (b for b in results
             if normalize_title(b["title"]) == wanted and _author_matches(s["author"], b["authors"])),
            None,
        )
        if match is None:
            filtered_out.append({**s, "why": "not found in Google Books"})
            continue
        if match["id"] in already_ranked_ids:
            filtered_out.append({**s, "why": "already ranked"})
            continue

        seen_titles.add(wanted)
        verified.append({**match, "reason": s["reason"]})

    return verified, filtered_out
