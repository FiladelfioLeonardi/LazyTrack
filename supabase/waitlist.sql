-- The website's email sign-up (docs/index.html): anyone can ADD their email, nobody can read the list through the
-- API (you see it in Supabase → Table Editor → waitlist). Paste into the SQL editor and Run, once. Safe to run again.
create table if not exists public.waitlist (
  id bigint generated always as identity primary key,
  email text not null unique check (char_length(email) between 5 and 254 and email like '%_@_%._%'),
  wants text not null default 'egg' check (wants in ('egg', 'app', 'both')),
  lang text not null default '' check (char_length(lang) <= 10),
  created_at timestamptz not null default now()
);
alter table public.waitlist enable row level security;
drop policy if exists "anyone can join the waitlist" on public.waitlist;
create policy "anyone can join the waitlist" on public.waitlist for insert to anon, authenticated with check (true);
-- no select / update / delete policies: the list stays private
notify pgrst, 'reload schema';
