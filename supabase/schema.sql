-- LazyTrack friends: profiles, friend requests, profile cards, chat.
-- Paste into the Supabase SQL editor. Safe to run again.
create table if not exists public.profiles (
  id uuid primary key references auth.users on delete cascade,
  username text not null,
  display_name text not null default '',
  created_at timestamptz not null default now()
);
create unique index if not exists profiles_username_key on public.profiles (lower(username));

create table if not exists public.cards (
  user_id uuid primary key references public.profiles on delete cascade,
  card jsonb not null default '{}',
  updated_at timestamptz not null default now()
);

create table if not exists public.friendships (
  id bigint generated always as identity primary key,
  requester uuid not null references public.profiles on delete cascade,
  addressee uuid not null references public.profiles on delete cascade,
  status text not null default 'pending' check (status in ('pending', 'accepted')),
  created_at timestamptz not null default now(),
  check (requester <> addressee),
  unique (requester, addressee)
);

create table if not exists public.messages (
  id bigint generated always as identity primary key,
  sender uuid not null references public.profiles on delete cascade,
  receiver uuid not null references public.profiles on delete cascade,
  body text not null check (char_length(body) between 1 and 1000),
  created_at timestamptz not null default now(),
  read_at timestamptz
);
create index if not exists messages_pair on public.messages (sender, receiver, created_at);

create or replace function public.are_friends(a uuid, b uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from friendships where status = 'accepted'
    and ((requester = a and addressee = b) or (requester = b and addressee = a)));
$$;

create or replace function public.username_taken(name text) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from profiles where lower(username) = lower(name));
$$;
grant execute on function public.username_taken(text) to anon, authenticated;
grant execute on function public.are_friends(uuid, uuid) to authenticated;

-- a new sign-up gets its profile row, with the username sent at sign-up
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into profiles (id, username, display_name)
  values (new.id, new.raw_user_meta_data->>'username',
          coalesce(new.raw_user_meta_data->>'display_name', new.raw_user_meta_data->>'username'));
  return new;
end $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

alter table public.profiles enable row level security;
alter table public.cards enable row level security;
alter table public.friendships enable row level security;
alter table public.messages enable row level security;

drop policy if exists "read profiles" on public.profiles;
create policy "read profiles" on public.profiles for select to authenticated using (true);
drop policy if exists "edit own profile" on public.profiles;
create policy "edit own profile" on public.profiles for update to authenticated
  using (id = auth.uid()) with check (id = auth.uid());

drop policy if exists "read own or friends card" on public.cards;
create policy "read own or friends card" on public.cards for select to authenticated
  using (user_id = auth.uid() or public.are_friends(user_id, auth.uid()));
drop policy if exists "write own card" on public.cards;
create policy "write own card" on public.cards for insert to authenticated with check (user_id = auth.uid());
drop policy if exists "update own card" on public.cards;
create policy "update own card" on public.cards for update to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists "see my friendships" on public.friendships;
create policy "see my friendships" on public.friendships for select to authenticated
  using (auth.uid() in (requester, addressee));
drop policy if exists "send a request" on public.friendships;
create policy "send a request" on public.friendships for insert to authenticated
  with check (requester = auth.uid() and status = 'pending');
drop policy if exists "accept a request" on public.friendships;
create policy "accept a request" on public.friendships for update to authenticated
  using (addressee = auth.uid()) with check (addressee = auth.uid() and status = 'accepted');
drop policy if exists "remove a friend" on public.friendships;
create policy "remove a friend" on public.friendships for delete to authenticated
  using (auth.uid() in (requester, addressee));
revoke update on public.friendships from authenticated;
grant update (status) on public.friendships to authenticated;

drop policy if exists "read my messages" on public.messages;
create policy "read my messages" on public.messages for select to authenticated
  using (auth.uid() in (sender, receiver));
drop policy if exists "send to a friend" on public.messages;
create policy "send to a friend" on public.messages for insert to authenticated
  with check (sender = auth.uid() and public.are_friends(sender, receiver));
drop policy if exists "mark as read" on public.messages;
create policy "mark as read" on public.messages for update to authenticated
  using (receiver = auth.uid()) with check (receiver = auth.uid());
revoke update on public.messages from authenticated;
grant update (read_at) on public.messages to authenticated;

-- make the API notice the new tables and functions right away
notify pgrst, 'reload schema';
