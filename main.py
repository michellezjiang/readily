"""Book review app: REST API.

Run:   uvicorn main:app --reload
Test:  open http://127.0.0.1:8000/docs in your browser
"""
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field
from supabase import Client

import books_api
import ranking
import recommender
from db import admin_client, public_client, user_client

app = FastAPI(title="Book Reviews API")

Sentiment = Literal["liked", "fine", "disliked"]


@app.exception_handler(books_api.BooksAPIError)
@app.exception_handler(recommender.RecommenderError)
def upstream_failed(request, exc: Exception):
    # 502 Bad Gateway: our server is fine, but a service we depend on
    # (Google Books or OpenAI) failed.
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# ---------- Request bodies (FastAPI validates these automatically → 422) ----------

class Credentials(BaseModel):
    email: str
    password: str


class SignUp(Credentials):
    username: str = Field(min_length=3, max_length=30)


class ReviewIn(BaseModel):
    book_id: str
    sentiment: Sentiment
    # Where the book goes inside its group. The client finds these by
    # binary-searching the list with the user ("which did you prefer?").
    above_book_id: str | None = Field(None, description="The book you liked a bit MORE. Omit if this is your new #1.")
    below_book_id: str | None = Field(None, description="The book you liked a bit LESS. Omit if this goes last.")
    body: str | None = None
    is_public: bool = True


# ---------- Auth helpers ----------

def _token_from(authorization: str | None) -> str | None:
    """Pull the token out of an 'Authorization: Bearer <token>' header."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1]
    return None


def require_user(authorization: str | None = Header(None)) -> tuple[Client, str]:
    """For endpoints that need a login. Returns (client acting as user, user id)."""
    token = _token_from(authorization)
    if not token:
        raise HTTPException(401, "Missing 'Authorization: Bearer <token>' header")
    client = user_client(token)
    try:
        user = client.auth.get_user(token).user  # Supabase checks the token is valid
    except Exception:
        raise HTTPException(401, "Invalid or expired token")
    return client, user.id


def optional_user(authorization: str | None = Header(None)) -> Client:
    """For endpoints that work logged out but show more when logged in."""
    token = _token_from(authorization)
    return user_client(token) if token else public_client()


def db_error(e: APIError) -> HTTPException:
    """Turn Postgres error codes into the right HTTP status codes."""
    status = {
        "23505": 409,  # unique violation: already reviewed / already following
        "42501": 403,  # RLS blocked it
        "23514": 400,  # check constraint failed
        "23503": 400,  # foreign key: referenced row doesn't exist
    }.get(e.code, 500)
    return HTTPException(status, e.message)


def user_id_for(client: Client, username: str) -> str:
    rows = client.table("profiles").select("id").eq("username", username).execute().data
    if not rows:
        raise HTTPException(404, f"No user named {username}")
    return rows[0]["id"]


# ---------- Auth endpoints ----------

@app.post("/auth/signup", status_code=201)
def signup(body: SignUp):
    # The username goes into user metadata, where the handle_new_user
    # trigger picks it up and creates the profile row.
    res = public_client().auth.sign_up({
        "email": body.email,
        "password": body.password,
        "options": {"data": {"username": body.username}},
    })
    return {"user_id": res.user.id if res.user else None}


@app.post("/auth/login")
def login(body: Credentials):
    try:
        res = public_client().auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except Exception:
        raise HTTPException(401, "Wrong email or password")
    return {"access_token": res.session.access_token, "user_id": res.user.id}


# ---------- Book endpoints ----------

RATING_FIELDS = "book_id, weighted_score, avg_score, review_count"
NO_RATING = {"weighted_score": None, "avg_score": None, "review_count": 0}


def ratings_for(book_ids: list[str]) -> dict[str, dict]:
    """Overall ratings for several books in ONE query (not one per book).
    Books nobody has publicly ranked yet simply aren't in the result."""
    if not book_ids:
        return {}
    rows = (public_client().table("book_rankings").select(RATING_FIELDS)
            .in_("book_id", book_ids).execute().data)
    return {row.pop("book_id"): row for row in rows}


@app.get("/books/search")
def search_books(q: str, limit: int = 10):
    """Search Google Books, with each result's overall rating attached."""
    books = books_api.search_books(q, min(limit, 40))
    ratings = ratings_for([b["id"] for b in books])
    return [{**book, **ratings.get(book["id"], NO_RATING)} for book in books]


@app.get("/books/{book_id}")
def get_book(book_id: str):
    """One book's details plus its overall rating."""
    cached = public_client().table("books").select("*").eq("id", book_id).execute().data
    book = cached[0] if cached else books_api.get_book(book_id)  # cache first, then Google
    if book is None:
        raise HTTPException(404, f"No book with id {book_id}")
    return {**book, **ratings_for([book_id]).get(book_id, NO_RATING)}


@app.get("/books/{book_id}/reviews")
def book_reviews(book_id: str, client: Client = Depends(optional_user)):
    # RLS decides which reviews come back: public ones, plus your own and
    # followers-only ones from people you follow if you're logged in.
    res = (
        client.table("ranked_reviews")
        .select("username, sentiment, score, body, is_public, created_at")
        .eq("book_id", book_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data


@app.get("/rankings")
def rankings(limit: int = 20):
    """Overall rankings: Bayesian average of everyone's public scores."""
    res = (
        public_client()
        .table("book_rankings")
        .select("*")
        .order("weighted_score", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data


# ---------- Personal rankings ----------

def ranked_list(client: Client, user_id: str, group: Sentiment | None) -> list[dict]:
    """One user's books, best first: liked, then fine, then disliked."""
    query = (
        client.table("ranked_reviews")
        .select("book_id, title, authors, cover_url, sentiment, score, list_position, body, is_public")
        .eq("user_id", user_id)
    )
    if group:
        query = query.eq("sentiment", group)
    rows = query.order("group_order").order("list_position").order("review_id").execute().data
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


@app.get("/me/rankings")
def my_rankings(group: Sentiment | None = None, auth=Depends(require_user)):
    """Your ranked list. Clients load one group, then binary-search it to
    place a new book."""
    client, user_id = auth
    return ranked_list(client, user_id, group)


@app.get("/users/{username}/rankings")
def user_rankings(username: str, group: Sentiment | None = None,
                  client: Client = Depends(optional_user)):
    """Someone else's ranked list. RLS hides their followers-only reviews
    unless you follow them."""
    return ranked_list(client, user_id_for(client, username), group)


# ---------- AI recommendations ----------

@app.get("/me/recommendations")
def my_recommendations(count: int = Query(5, ge=1, le=10), auth=Depends(require_user)):
    """Book suggestions from an LLM, based on your rankings, with every
    suggestion checked against Google Books before it's returned."""
    client, user_id = auth
    ranked = ranked_list(client, user_id, None)

    liked = [r for r in ranked if r["sentiment"] == "liked"][:10]       # your top 10
    disliked = [r for r in ranked if r["sentiment"] == "disliked"][-5:]  # your bottom 5
    if not liked:
        raise HTTPException(400, "Rank at least one book you liked first.")

    # Ask for a few extra, since some get filtered out during verification.
    prompt = recommender.build_prompt(liked, disliked, count + 3)
    suggestions = recommender.parse_suggestions(recommender.ask_llm(prompt))

    verified, filtered_out = recommender.verify(
        suggestions,
        already_ranked_ids={r["book_id"] for r in ranked},
        already_ranked_titles={r["title"] for r in ranked},
        search=books_api.search_books,
        count=count,
    )
    ratings = ratings_for([b["id"] for b in verified])
    return {
        "recommendations": [{**b, **ratings.get(b["id"], NO_RATING)} for b in verified],
        "filtered_out": filtered_out,  # what the model got wrong, and why
    }


# ---------- Reviews ----------

def ensure_book_cached(book_id: str) -> None:
    """Make sure the book is in our books table, verifying it with Google
    if it isn't. Raises 404 for IDs Google doesn't know."""
    cached = public_client().table("books").select("id").eq("id", book_id).execute().data
    if cached:
        return  # cache hit: no call to Google needed

    book = books_api.get_book(book_id)
    if book is None:
        raise HTTPException(404, f"No book with id {book_id}")
    # Users have no insert permission on books, so the server writes it with
    # the admin client. Only this path, after checking Google, adds books.
    admin_client().table("books").upsert(book, ignore_duplicates=True).execute()


@app.post("/reviews", status_code=201)
def create_review(review: ReviewIn, auth=Depends(require_user)):
    client, user_id = auth

    already = (client.table("reviews").select("id")
               .eq("user_id", user_id).eq("book_id", review.book_id).execute().data)
    if already:
        raise HTTPException(409, "You've already ranked this book. Delete it first to re-rank it.")

    ensure_book_cached(review.book_id)

    # Load this group as it is now, best first.
    group = (
        client.table("reviews").select("book_id, list_position")
        .eq("user_id", user_id).eq("sentiment", review.sentiment)
        .order("list_position").order("id")
        .execute().data
    )
    try:
        position, renumbered = ranking.plan_insert(
            [(row["book_id"], row["list_position"]) for row in group],
            review.above_book_id,
            review.below_book_id,
        )
    except ranking.StaleListError as e:
        # 409 Conflict: the request doesn't match the current state of the list.
        raise HTTPException(409, str(e))

    try:
        # Rare: only after ~50 inserts into the same gap.
        for book_id, new_position in renumbered.items():
            (client.table("reviews").update({"list_position": new_position})
             .eq("user_id", user_id).eq("book_id", book_id).execute())

        # Written AS THE USER, so RLS checks user_id matches the logged-in user.
        client.table("reviews").insert({
            "user_id": user_id,
            "book_id": review.book_id,
            "sentiment": review.sentiment,
            "list_position": position,
            "body": review.body,
            "is_public": review.is_public,
        }).execute()
    except APIError as e:
        raise db_error(e)

    created = (client.table("ranked_reviews")
               .select("book_id, title, sentiment, score, list_position, body, is_public")
               .eq("user_id", user_id).eq("book_id", review.book_id).execute().data)
    return created[0]


@app.delete("/reviews/{book_id}", status_code=204)
def delete_review(book_id: str, auth=Depends(require_user)):
    client, user_id = auth
    res = client.table("reviews").delete().eq("user_id", user_id).eq("book_id", book_id).execute()
    if not res.data:
        raise HTTPException(404, "You haven't ranked this book")
    return Response(status_code=204)


# ---------- Following ----------

@app.post("/users/{username}/follow", status_code=204)
def follow(username: str, auth=Depends(require_user)):
    client, user_id = auth
    try:
        client.table("follows").insert({
            "follower_id": user_id,
            "following_id": user_id_for(client, username),
        }).execute()
    except APIError as e:
        raise db_error(e)  # 409 if already following, 400 if following yourself
    return Response(status_code=204)


@app.delete("/users/{username}/follow", status_code=204)
def unfollow(username: str, auth=Depends(require_user)):
    client, user_id = auth
    (client.table("follows").delete()
     .eq("follower_id", user_id).eq("following_id", user_id_for(client, username)).execute())
    return Response(status_code=204)
