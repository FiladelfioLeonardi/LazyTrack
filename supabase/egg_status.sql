-- LazyTrack Egg: its status (so LazyTrack on your computer and phone can show it) and commands for it
-- ("make him sing", "find my egg", "sync now"). Paste into the Supabase SQL editor after egg.sql. Safe to run again.

-- 1. What the egg says about itself, about once a minute. One row per person, only you can see or write it.
create table if not exists public.egg_status (
  user_id uuid primary key default auth.uid() references public.profiles on delete cascade,
  seen_at timestamptz not null default now(),
  via text not null default '' check (length(via) <= 40),          -- 'wifi' / 'phone'
  wifi text not null default '' check (length(wifi) <= 40),        -- the WiFi's name
  phones int not null default 0 check (phones between 0 and 20),   -- how many phones are paired
  phone_linked boolean not null default false,                     -- a paired phone is connected right now
  screen text not null default '' check (length(screen) <= 20),    -- 'home', 'menu', 'off'...
  asleep boolean not null default false,
  waiting int not null default 0 check (waiting between 0 and 1000), -- logs waiting to be sent
  free_kb int not null default 0 check (free_kb between 0 and 100000),
  fw text not null default '' check (length(fw) <= 40)
);
alter table public.egg_status enable row level security;
drop policy if exists "own egg status read" on public.egg_status;
create policy "own egg status read" on public.egg_status for select to authenticated using (user_id = auth.uid());
drop policy if exists "own egg status add" on public.egg_status;
create policy "own egg status add" on public.egg_status for insert to authenticated with check (user_id = auth.uid());
drop policy if exists "own egg status change" on public.egg_status;
create policy "own egg status change" on public.egg_status for update to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());

-- 2. Commands for your egg, from LazyTrack (computer or phone). The egg picks them up within ~10 s and deletes them.
create table if not exists public.egg_commands (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references public.profiles on delete cascade,
  cmd text not null check (cmd in ('sing', 'find', 'sync')),
  created_at timestamptz not null default now()
);
create index if not exists egg_commands_user on public.egg_commands (user_id, id);
alter table public.egg_commands enable row level security;
drop policy if exists "own egg commands read" on public.egg_commands;
create policy "own egg commands read" on public.egg_commands for select to authenticated using (user_id = auth.uid());
drop policy if exists "own egg commands add" on public.egg_commands;
create policy "own egg commands add" on public.egg_commands for insert to authenticated
  with check (user_id = auth.uid() and (select count(*) from public.egg_commands c where c.user_id = auth.uid()) < 20);
drop policy if exists "own egg commands delete" on public.egg_commands;
create policy "own egg commands delete" on public.egg_commands for delete to authenticated using (user_id = auth.uid());

notify pgrst, 'reload schema';
