-- =====================================================================
-- Migration 001: personal rankings (Beli-style)
--
-- Run this ONCE in the Supabase SQL Editor on the database you already
-- set up. It replaces 1-5 star ratings with ordered lists:
--   reviews.rating   ->  reviews.sentiment + reviews.list_position
-- Existing reviews are kept and converted (4-5 stars -> liked,
-- 3 -> fine, 1-2 -> disliked, ordered by their old rating).
-- The whole thing runs as one transaction: it either fully succeeds or
-- changes nothing.
-- =====================================================================

begin;

-- 1. The old ranking view depends on reviews.rating, so drop it first.
drop view if exists public.book_rankings;

-- 2. Add the new columns (nullable for now so existing rows are allowed).
alter table public.reviews
  add column sentiment text check (sentiment in ('liked', 'fine', 'disliked')),
  add column list_position double precision;

-- 3. Convert existing reviews.
update public.reviews
set sentiment = case
  when rating >= 4 then 'liked'
  when rating = 3  then 'fine'
  else 'disliked'
end;

with ordered as (
  select id,
         row_number() over (partition by user_id, sentiment
                            order by rating desc, created_at) as rn
  from public.reviews
)
update public.reviews r
set list_position = o.rn
from ordered o
where o.id = r.id;

-- 4. Now every row has values: make them required, drop the old column.
alter table public.reviews
  alter column sentiment set not null,
  alter column list_position set not null,
  drop column rating;

create index reviews_user_list_idx on public.reviews (user_id, sentiment, list_position);

-- 5. Scores and rankings (same as section 4 of supabase_schema.sql).
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

commit;
