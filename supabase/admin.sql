-- LazyTrack admin: the ideas box (+ button), announcements, and the numbers behind the Admin page.
-- Paste the whole file into the Supabase SQL editor and press Run. Safe to run again.
-- Needs schema.sql (the friends tables) to be there already.
-- >>> Put YOUR LazyTrack username on the next line: that account becomes the admin. <<<
create temp table if not exists _me as select 'fefo420'::text as username;


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

drop policy if exists "admins delete ideas" on public.ideas;
create policy "admins delete ideas" on public.ideas for delete to authenticated using (public.is_admin());

-- announcements: a short message on everyone's Home (new feature, maintenance, ...)
create table if not exists public.announcements (
  id bigint generated always as identity primary key,
  body text not null check (char_length(body) between 3 and 280),
  kind text not null default 'info' check (kind in ('info', 'new', 'warn')),
  active boolean not null default true,
  created_at timestamptz not null default now()
);
alter table public.announcements enable row level security;
drop policy if exists "everyone reads live announcements" on public.announcements;
create policy "everyone reads live announcements" on public.announcements for select to authenticated
  using (active or public.is_admin());
drop policy if exists "admins write announcements" on public.announcements;
create policy "admins write announcements" on public.announcements for all to authenticated
  using (public.is_admin()) with check (public.is_admin());

-- the Admin page's numbers. Only counts and Mochi info: nobody's habit stats.
create or replace function public.admin_stats() returns jsonb
language plpgsql stable security definer set search_path = public as $$
begin
  if not public.is_admin() then raise exception 'admins only'; end if;
  return jsonb_build_object(
    'users',       (select count(*) from profiles),
    'new_7d',      (select count(*) from profiles where created_at > now() - interval '7 days'),
    'active_1d',   (select count(*) from cards where updated_at > now() - interval '1 day'),
    'active_7d',   (select count(*) from cards where updated_at > now() - interval '7 days'),
    'friendships', (select count(*) from friendships where status = 'accepted'),
    'messages_7d', (select count(*) from messages where created_at > now() - interval '7 days'),
    'ideas',    (select coalesce(jsonb_object_agg(s.status, s.n), '{}'::jsonb) from (select i.status, count(*) n from ideas i group by i.status) s),
    'versions', (select coalesce(jsonb_object_agg(s.v, s.n), '{}'::jsonb) from (select coalesce(c.card->'app'->>'v', 'older') v, count(*) n
                   from cards c where c.updated_at > now() - interval '7 days' group by 1) s),
    'os',       (select coalesce(jsonb_object_agg(s.o, s.n), '{}'::jsonb) from (select coalesce(c.card->'app'->>'os', '?') o, count(*) n
                   from cards c where c.updated_at > now() - interval '7 days' group by 1) s),
    'signups',  (select coalesce(jsonb_agg(jsonb_build_object('d', s.d, 'n', s.n) order by s.d), '[]'::jsonb)
                   from (select p.created_at::date d, count(*) n from profiles p where p.created_at > now() - interval '30 days' group by 1) s)
  );
end $$;
grant execute on function public.admin_stats() to authenticated;

create or replace function public.admin_users() returns jsonb
language plpgsql stable security definer set search_path = public as $$
begin
  if not public.is_admin() then raise exception 'admins only'; end if;
  return (select coalesce(jsonb_agg(u order by u.seen desc nulls last), '[]'::jsonb) from (
    select p.id, p.username, p.display_name name, p.created_at joined, c.updated_at seen,
           case when jsonb_typeof(c.card->'hp') = 'number' then round((c.card->>'hp')::numeric) end hp,
           c.card->>'tier' tier, c.card->'gen' gen, c.card->'age' age, c.card->'deaths' deaths,
           c.card->'app'->>'v' version, c.card->'app'->>'os' os,
           (select count(*) from ideas i where i.user_id = p.id) ideas,
           (select count(*) from friendships f where f.status = 'accepted' and p.id in (f.requester, f.addressee)) friends,
           exists (select 1 from admins a where a.user_id = p.id) admin
    from profiles p left join cards c on c.user_id = p.id
    order by c.updated_at desc nulls last limit 500) u);
end $$;
grant execute on function public.admin_users() to authenticated;

-- make the username at the top the admin
insert into public.admins (user_id)
  select p.id from public.profiles p, _me where lower(p.username) = lower(_me.username)
  on conflict do nothing;

notify pgrst, 'reload schema';
