"""Connecting to Supabase.

Three kinds of clients:
- public_client(): no logged-in user. RLS treats requests as "anon",
  so they only see public data.
- user_client(token): acts as the logged-in user. RLS sees their ID
  through auth.uid(), so all of your policies apply to them.
- admin_client(): uses the service_role key, which BYPASSES RLS.
  Only for writes the server itself must vouch for. Right now that's
  just caching books it has verified against Google Books.

The service_role key stays in this server's .env. Never put it in a
frontend, never commit it to GitHub.
"""
import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()  # reads the SUPABASE_* values from .env

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]


def public_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def user_client(access_token: str) -> Client:
    # A fresh client per request, so two users' requests never share a token.
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    client.postgrest.auth(access_token)  # database queries now run as this user
    return client


_admin: Client | None = None


def admin_client() -> Client:
    global _admin
    if _admin is None:
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not key:
            raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is missing from .env")
        _admin = create_client(SUPABASE_URL, key)
    return _admin
