-- Makes sure ONLY your account is the admin (removes anyone else from the admin list).
-- Paste into the Supabase SQL editor and press Run. Safe to run again.
-- >>> Your LazyTrack username: <<<
create temp table if not exists _me_lock as select 'fefo420'::text as username;

delete from public.admins
 where user_id not in (select p.id from public.profiles p, _me_lock m where lower(p.username) = lower(m.username));
insert into public.admins (user_id)
  select p.id from public.profiles p, _me_lock m where lower(p.username) = lower(m.username)
  on conflict do nothing;

-- who's admin now (should be just you):
select p.username from public.admins a join public.profiles p on p.id = a.user_id;
