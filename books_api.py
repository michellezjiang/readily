"""Talking to the Google Books API.

Works without a key for light use, but Google often rate-limits keyless
requests (HTTP 429). If that happens, set GOOGLE_BOOKS_API_KEY in .env.
"""
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"
API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY")


class BooksAPIError(Exception):
    """Google Books failed or couldn't be reached."""


def _get(url: str, params: dict | None = None) -> httpx.Response:
    params = dict(params or {})
    if API_KEY:
        params["key"] = API_KEY
    try:
        return httpx.get(url, params=params, timeout=10)
    except httpx.RequestError as e:
        raise BooksAPIError(f"Couldn't reach Google Books: {e}") from e


def _check(resp: httpx.Response) -> None:
    if resp.is_error:
        raise BooksAPIError(f"Google Books returned {resp.status_code}: {resp.text[:300]}")


def _to_book(item: dict) -> dict:
    """Trim Google's large response down to the columns in our books table."""
    info = item.get("volumeInfo", {})
    year = (info.get("publishedDate") or "")[:4]
    return {
        "id": item["id"],
        "title": info.get("title", "Untitled"),
        "authors": info.get("authors", []),
        "description": info.get("description"),
        "cover_url": info.get("imageLinks", {}).get("thumbnail"),
        "published_year": int(year) if year.isdigit() else None,
    }


def search_books(query: str, limit: int = 10) -> list[dict]:
    resp = _get(GOOGLE_BOOKS, {"q": query, "maxResults": limit})
    _check(resp)
    return [_to_book(item) for item in resp.json().get("items", [])]


def get_book(book_id: str) -> dict | None:
    """Look up one book by its Google Books ID. None if it doesn't exist."""
    resp = _get(f"{GOOGLE_BOOKS}/{book_id}")
    if resp.status_code == 404:
        return None
    _check(resp)
    return _to_book(resp.json())
