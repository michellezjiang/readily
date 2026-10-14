-- =====================================================================
-- Book review app — Supabase schema + Row Level Security
-- Paste into the Supabase SQL editor (or save as a migration).
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. TABLES
-- ---------------------------------------------------------------------

-- One row per user. Supabase manages login in auth.users; this table
-- holds the public-facing profile info.
create table public.profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  username     text unique check (char_length(username) between 3 and 30),
  display_name text,
  created_at   timestamptz not null default now()
);

-- Local cache of books fetched from the external API (Google Books /
-- Open Library). The primary key IS the external ID, so the same book
-- can never be cached twice and we skip the API call once it's stored.
create table public.books (
  id             text primary key,          -- e.g. Google Books volume ID
  title          text not null,
  authors        text[] not null default '{}',
  description    text,                       -- synopsis
  cover_url      text,
  published_year int,
  fetched_at     timestamptz not null default now()
);

-- One review per user per book (enforced by the unique constraint).
-- Instead of a star rating, each review records:
--   sentiment      which group the book is in: liked / fine / disliked
--   list_position  where it sits inside that group. LOWER = ranked higher.
--                  It's a decimal so a book can be inserted between two
--                  others (e.g. 1.5 between 1 and 2) by updating one row.
-- A 0-10 score is DERIVED from these (see section 4); it is never stored.
create table public.reviews (
  id            bigint generated always as identity primary key,
  user_id       uuid not null references public.profiles (id) on delete cascade,
  book_id       text not null references public.books (id) on delete cascade,
  sentiment     text not null check (sentiment in ('liked', 'fine', 'disliked')),
  list_position double precision not null,
  body          text,
  is_public  boolean not null default true,  -- false = followers only
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, book_id)
);

-- Who follows whom. The composite primary key stops duplicate follows.
create table public.follows (
  follower_id  uuid not null references public.profiles (id) on delete cascade,
  following_id uuid not null references public.profiles (id) on delete cascade,
  created_at   timestamptz not null default now(),
  primary key (follower_id, following_id),
  check (follower_id <> following_id)        -- can't follow yourself
);

-- Indexes for the lookups the app does most.
-- (follows.follower_id is already covered by the primary key.)
create index reviews_book_id_idx      on public.reviews (book_id);
create index reviews_user_list_idx    on public.reviews (user_id, sentiment, list_position);
create index follows_following_id_idx on public.follows (following_id);


-- ---------------------------------------------------------------------
-- 2. TRIGGERS
-- ---------------------------------------------------------------------

-- Create a profile automatically when someone signs up.
create function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = ''
as $$
begin
  insert into public.profiles (id, username)
  values (new.id, new.raw_user_meta_data ->> 'username');
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Keep reviews.updated_at accurate when a review is edited.
create function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger reviews_set_updated_at
  before update on public.reviews
  for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------
-- 3. ROW LEVEL SECURITY
-- With RLS on, every query is denied unless a policy allows it.
-- auth.uid() is the logged-in user's ID (null when logged out).
-- It's wrapped in (select ...) so Postgres evaluates it once per query
-- instead of once per row — Supabase's recommended pattern.
-- ---------------------------------------------------------------------

alter table public.profiles enable row level security;
alter table public.books    enable row level security;
alter table public.reviews  enable row level security;
alter table public.follows  enable row level security;

-- PROFILES: anyone can view; you can only edit your own.
create policy "profiles are viewable by everyone"
  on public.profiles for select
  using (true);

create policy "users update own profile"
  on public.profiles for update to authenticated
  using      (id = (select auth.uid()))
  with check (id = (select auth.uid()));

-- BOOKS: anyone can read the cache. There is deliberately NO insert,
-- update, or delete policy, so no user can write to this table through
-- the public API. Only the Python server adds books, using the
-- service_role key, after verifying the book ID with Google Books.
-- That makes fake books impossible.
create policy "books are viewable by everyone"
  on public.books for select
  using (true);

-- REVIEWS: you see a review if it's public, it's yours, or you follow
-- its author. You can only write/edit/delete your own.
create policy "reviews visible if public, own, or followed"
  on public.reviews for select
  using (
    is_public
    or user_id = (select auth.uid())
    or exists (
      select 1 from public.follows f
      where f.follower_id  = (select auth.uid())
        and f.following_id = reviews.user_id
    )
  );

create policy "users create own reviews"
  on public.reviews for insert to authenticated
  with check (user_id = (select auth.uid()));

create policy "users update own reviews"
  on public.reviews for update to authenticated
  using      (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

create policy "users delete own reviews"
  on public.reviews for delete to authenticated
  using (user_id = (select auth.uid()));

-- FOLLOWS: the follow graph is public; you control only your own follows.
create policy "follows are viewable by everyone"
  on public.follows for select
  using (true);

create policy "users follow as themselves"
  on public.follows for insert to authenticated
  with check (follower_id = (select auth.uid()));

create policy "users unfollow as themselves"
  on public.follows for delete to authenticated
  using (follower_id = (select auth.uid()));


-- ---------------------------------------------------------------------
-- 4. SCORES AND RANKINGS
-- ---------------------------------------------------------------------

-- 4a. Personal scores (0-10), derived from each user's ordered lists.
--
-- Each group has its own score band:
--     liked     6.7 - 10
--     fine      3.4 - 6.6
--     disliked  0   - 3.3
-- Inside a group with n books, the book at rank r (1 = best) gets
--     score = top_of_band - band_width * (r - 1) / n
-- so the #1 book gets the top of the band, and books spread evenly below.
-- Inserting a new book shifts the scores of the books below it. That's
-- intended: your scores are always relative to the rest of your list.
--
-- WHY THIS LIVES IN A FUNCTION IN A "private" SCHEMA:
-- A book's rank depends on the user's WHOLE list, including followers-only
-- reviews. If we computed ranks under each viewer's RLS, a stranger (who
-- can't see the private reviews) would get different ranks, and different
-- scores, than a follower would. So this function runs as security definer,
-- ranking over every review so scores are the same for every viewer. It
-- returns only (review_id, score); the views below join it back to
-- public.reviews, where RLS still decides which reviews you can see.
-- The "private" schema is not exposed through Supabase's REST API, so no
-- one can call this function directly.
-- (Small trade-off: a public review's score can hint that hidden reviews
--  rank above it. Acceptable for book opinions.)

create schema if not exists private;
grant usage on schema private to anon, authenticated;

create function private.review_scores()
returns table (review_id bigint, group_order int, score numeric)
language sql
stable
security definer set search_path = ''
as $$
  select
    r.id,
    b.group_order,
    b.hi - (b.hi - b.lo)
           * (row_number() over (partition by r.user_id, r.sentiment
                                 order by r.list_position, r.id) - 1)
           / count(*)    over (partition by r.user_id, r.sentiment)
  from public.reviews r
  join (values ('liked',    1, 10.0, 6.7),
               ('fine',     2,  6.6, 3.4),
               ('disliked', 3,  3.3, 0.0)) as b (sentiment, group_order, hi, lo)
    on b.sentiment = r.sentiment
$$;

grant execute on function private.review_scores() to anon, authenticated;

-- 4b. Reviews with their scores, plus the book and reviewer info the app
-- needs. This is what the API reads. security_invoker = true means RLS on
-- reviews still applies: you see public reviews, your own, and
-- followers-only reviews from people you follow.
create view public.ranked_reviews
with (security_invoker = true) as
select
  r.id               as review_id,
  r.user_id,
  p.username,
  r.book_id,
  b.title,
  b.authors,
  b.cover_url,
  r.sentiment,
  s.group_order,     -- 1 liked, 2 fine, 3 disliked (for sorting)
  r.list_position,
  round(s.score, 1)  as score,
  r.body,
  r.is_public,
  r.created_at,
  r.updated_at
from public.reviews r
join private.review_scores() s on s.review_id = r.id
join public.books b            on b.id = r.book_id
join public.profiles p         on p.id = r.user_id;

-- 4c. Overall book rankings (Bayesian average of personal scores).
--
-- weighted_score = (C * global_mean + n * book_mean) / (C + n)
--
-- A book with few reviews is pulled toward the site-wide average; as it
-- gets more reviews, its own average dominates. C = 10 means "act as if
-- every book starts with 10 average-scored reviews." Tune C as needed.
--
-- Only public reviews count, so the rankings are the same for every viewer.
create view public.book_rankings
with (security_invoker = true) as
with public_scores as (
  select r.book_id, s.score
  from public.reviews r
  join private.review_scores() s on s.review_id = r.id
  where r.is_public
),
global as (
  select avg(score) as global_mean from public_scores
),
per_book as (
  select book_id,
         count(*)   as review_count,
         avg(score) as book_mean
  from public_scores
  group by book_id
)
select
  b.id                     as book_id,
  b.title,
  b.authors,
  b.cover_url,
  p.review_count,
  round(p.book_mean, 2)    as avg_score,
  round((10 * g.global_mean + p.review_count * p.book_mean)
        / (10 + p.review_count), 3) as weighted_score
from per_book p
join public.books b on b.id = p.book_id
cross join global g;
