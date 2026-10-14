"""Terminal demo of Beli-style ranking against your running API.

Usage (with `uvicorn main:app --reload` running in another terminal):
    python rank_cli.py

It logs you in, lets you search for a book, asks "which did you prefer?"
a few times (binary search), then posts the review to POST /reviews.
This plays the role a frontend would: the comparisons happen on the
client, and the server only receives the final neighbors.
"""
import getpass

import httpx

from ranking import find_insert_index

API = "http://127.0.0.1:8000"
GROUPS = {"1": "liked", "2": "fine", "3": "disliked"}


def main():
    email = input("Email: ")
    password = getpass.getpass("Password: ")
    login = httpx.post(f"{API}/auth/login", json={"email": email, "password": password})
    login.raise_for_status()
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    results = httpx.get(f"{API}/books/search", params={"q": input("Search for a book: ")}, timeout=15)
    results.raise_for_status()
    books = results.json()
    if not books:
        print("No results.")
        return
    for i, b in enumerate(books, 1):
        print(f"  {i}. {b['title']} — {', '.join(b['authors']) or 'unknown author'}")
    book = books[int(input("Pick a number: ")) - 1]

    group = GROUPS[input("How was it? 1) liked it  2) it was fine  3) didn't like it: ").strip()]
    ranked = httpx.get(f"{API}/me/rankings", params={"group": group}, headers=headers).json()

    def prefers_new_over(existing):
        answer = input(f"Which did you prefer?  1) {book['title']}   2) {existing['title']}: ")
        return answer.strip() == "1"

    index = find_insert_index(ranked, prefers_new_over)
    above = ranked[index - 1]["book_id"] if index > 0 else None
    below = ranked[index]["book_id"] if index < len(ranked) else None

    resp = httpx.post(f"{API}/reviews", headers=headers, timeout=15, json={
        "book_id": book["id"], "sentiment": group,
        "above_book_id": above, "below_book_id": below,
    })
    if resp.is_error:
        print(f"Error {resp.status_code}: {resp.json().get('detail')}")
        return
    print(f"\nRanked! Score: {resp.json()['score']}\n\nYour '{group}' list:")
    for row in httpx.get(f"{API}/me/rankings", params={"group": group}, headers=headers).json():
        print(f"  {row['rank']}. {row['title']}  ({row['score']})")


if __name__ == "__main__":
    main()
