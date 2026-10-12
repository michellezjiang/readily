"""Book review app: REST API.

Run:   uvicorn main:app --reload
Test:  open http://127.0.0.1:8000/docs in your browser
"""
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field
from supabase import Client

import books_api
from db import public_client, user_client

app = FastAPI(title="Book Reviews API")


@app.exception_handler(books_api.BooksAPIError)
def books_api_failed(request, exc: books_api.BooksAPIError):
    # 502 Bad Gateway: our server is fine, but a service we depend on failed.
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# ---------- Request bodies (FastAPI validates these automatically → 422) ----------

class Credentials(BaseModel):
    email: str
    password: str


class SignUp(Credentials):
    username: str = Field(min_length=3, max_length=30)


class ReviewIn(BaseModel):
    book_id: str
    rating: int = Field(ge=1, le=5)
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
        "23505": 409,  # unique violation: already reviewed this book
        "42501": 403,  # RLS blocked it
        "23514": 400,  # check constraint failed
        "23503": 400,  # foreign key: referenced row doesn't exist
    }.get(e.code, 500)
    return HTTPException(status, e.message)


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

@app.get("/books/search")
def search_books(q: str, limit: int = 10):
    return books_api.search_books(q, min(limit, 40))


@app.get("/books/{book_id}/reviews")
def book_reviews(book_id: str, client: Client = Depends(optional_user)):
    # RLS decides which reviews come back: public ones, plus your own and
    # followers-only ones from people you follow if you're logged in.
    res = (
        client.table("reviews")
        .select("id, rating, body, is_public, created_at, profiles(username)")
        .eq("book_id", book_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data


@app.get("/rankings")
def rankings(limit: int = 20):
    res = (
        public_client()
        .table("book_rankings")
        .select("*")
        .order("weighted_score", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data


# ---------- Review endpoints ----------

@app.post("/reviews", status_code=201)
def create_review(review: ReviewIn, auth=Depends(require_user)):
    client, user_id = auth

    # Fetch the book from Google ourselves instead of trusting the client to
    # send its title/author. This closes the "fake book" gap.
    book = books_api.get_book(review.book_id)
    if book is None:
        raise HTTPException(404, f"No book with id {review.book_id}")

    try:
        # Cache the book; do nothing if it's already cached.
        client.table("books").upsert(book, ignore_duplicates=True).execute()
        res = client.table("reviews").insert({
            "user_id": user_id,
            "book_id": review.book_id,
            "rating": review.rating,
            "body": review.body,
            "is_public": review.is_public,
        }).execute()
    except APIError as e:
        raise db_error(e)
    return res.data[0]