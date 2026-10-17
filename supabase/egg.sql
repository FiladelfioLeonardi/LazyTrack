-- LazyTrack Egg: logging from the egg, and pokes between friends (from the egg or the app).
-- Paste into the Supabase SQL editor after schema.sql. Safe to run again.

-- 1. Things logged on the egg wait here until your LazyTrack picks them up (every ~30 s), adds them
--    to your history and deletes them. Only you can see or add yours.
create table if not exists public.egg_inbox (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references public.profiles on delete cascade,
  kind text not null check (kind ~ '^[a-z0-9_]{1,40}$'),
  ts timestamptz not null default now()
);
alter table public.egg_inbox enable row level security;
drop policy if exists "own inbox read" on public.egg_inbox;
create policy "own inbox read" on public.egg_inbox for select to authenticated using (user_id = auth.uid());
drop policy if exists "own inbox add" on public.egg_inbox;
create policy "own inbox add" on public.egg_inbox for insert to authenticated with check (user_id = auth.uid());
drop policy if exists "own inbox delete" on public.egg_inbox;
create policy "own inbox delete" on public.egg_inbox for delete to authenticated using (user_id = auth.uid());

-- 2. What the egg's "Log" menu offers: your habits, put here by LazyTrack. Private (not on your friend card).
create table if not exists public.egg_setup (
  user_id uuid primary key default auth.uid() references public.profiles on delete cascade,
  habits jsonb not null default '[]',
  updated_at timestamptz not null default now()
);
alter table public.egg_setup enable row level security;
drop policy if exists "own egg setup read" on public.egg_setup;
create policy "own egg setup read" on public.egg_setup for select to authenticated using (user_id = auth.uid());
drop policy if exists "own egg setup add" on public.egg_setup;
create policy "own egg setup add" on public.egg_setup for insert to authenticated with check (user_id = auth.uid());
drop policy if exists "own egg setup change" on public.egg_setup;
create policy "own egg setup change" on public.egg_setup for update to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());

-- 3. Pokes: a friend pokes (or sends love to) your Mochi. Your egg buzzes and your app plays a sound.
--    The egg and the app each mark it seen on their own, so both get it.
create table if not exists public.pokes (
  id bigint generated always as identity primary key,
  sender uuid not null default auth.uid() references public.profiles on delete cascade,
  receiver uuid not null references public.profiles on delete cascade,
  kind text not null default 'poke' check (kind in ('poke', 'love', 'wave')),
  created_at timestamptz not null default now(),
  seen_app timestamptz,
  seen_egg timestamptz,
  check (sender <> receiver)
);
create index if not exists pokes_receiver on public.pokes (receiver, created_at);

-- at most 10 pokes a minute from one person, so nobody can spam an egg
create or replace function public.can_poke(target uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select public.are_friends(auth.uid(), target)
     and (select count(*) from pokes where sender = auth.uid() and created_at > now() - interval '1 minute') < 10;
$$;
grant execute on function public.can_poke(uuid) to authenticated;

alter table public.pokes enable row level security;
drop policy if exists "see my pokes" on public.pokes;
create policy "see my pokes" on public.pokes for select to authenticated using (auth.uid() in (sender, receiver));
drop policy if exists "poke a friend" on public.pokes;
create policy "poke a friend" on public.pokes for insert to authenticated
  with check (sender = auth.uid() and public.can_poke(receiver));
drop policy if exists "mark pokes seen" on public.pokes;
create policy "mark pokes seen" on public.pokes for update to authenticated
  using (receiver = auth.uid()) with check (receiver = auth.uid());
drop policy if exists "clear old pokes" on public.pokes;
create policy "clear old pokes" on public.pokes for delete to authenticated using (auth.uid() in (sender, receiver));
revoke update on public.pokes from authenticated;
grant update (seen_app, seen_egg) on public.pokes to authenticated;

notify pgrst, 'reload schema';
