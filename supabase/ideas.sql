-- LazyTrack ideas box: people suggest features from the app (the + button), you read them in
-- Settings -> App -> Ideas from people. Paste into the Supabase SQL editor. Safe to run again.

create table if not exists public.ideas (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references public.profiles on delete cascade,
  title text not null check (char_length(title) between 3 and 120),
  body text not null default '' check (char_length(body) <= 2000),
  area text not null default 'other',
  want smallint not null default 2 check (want between 1 and 3),   -- 1 nice to have, 2 want it, 3 need it
  platform text not null default '',
  version text not null default '',
  status text not null default 'new' check (status in ('new', 'planned', 'done', 'no')),
  created_at timestamptz not null default now()
);
create index if not exists ideas_created on public.ideas (created_at desc);

-- who can read every idea and change its status (you)
create table if not exists public.admins (
  user_id uuid primary key references public.profiles on delete cascade
);

create or replace function public.is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from admins where user_id = auth.uid());
$$;
grant execute on function public.is_admin() to authenticated;

alter table public.ideas enable row level security;
alter table public.admins enable row level security;

drop policy if exists "send own ideas" on public.ideas;
create policy "send own ideas" on public.ideas for insert to authenticated with check (user_id = auth.uid());
drop policy if exists "read own ideas, admins read all" on public.ideas;
create policy "read own ideas, admins read all" on public.ideas for select to authenticated
  using (user_id = auth.uid() or public.is_admin());
drop policy if exists "admins set status" on public.ideas;
create policy "admins set status" on public.ideas for update to authenticated
  using (public.is_admin()) with check (public.is_admin());
drop policy if exists "see yourself in admins" on public.admins;
create policy "see yourself in admins" on public.admins for select to authenticated using (user_id = auth.uid());

-- make yourself the admin: put your LazyTrack username here
insert into public.admins (user_id)
  select id from public.profiles where lower(username) = lower('YOUR_USERNAME')
  on conflict do nothing;

notify pgrst, 'reload schema';
