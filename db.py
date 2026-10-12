"""Connecting to Supabase.

Two kinds of clients:
- public_client(): no logged-in user. RLS treats requests as "anon",
  so they only see public data.
- user_client(token): acts as the logged-in user. RLS sees their ID
  through auth.uid(), so all of your policies apply to them.

The app only ever uses the anon/publishable key. Never the service_role
key: that one bypasses RLS entirely.
"""
import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()  # reads SUPABASE_URL and SUPABASE_ANON_KEY from .env

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]


def public_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def user_client(access_token: str) -> Client:
    # A fresh client per request, so two users' requests never share a token.
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    client.postgrest.auth(access_token)  # database queries now run as this user
    return client