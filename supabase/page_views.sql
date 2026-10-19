create table if not exists public.page_views (
  id bigint generated always as identity primary key,
  day date not null default current_date,
  page text not null default '' check (char_length(page) <= 40),
  event text not null default 'visit' check (event in ('visit', 'download-mac', 'download-win')),
  source text not null default '' check (char_length(source) <= 60),
  created_at timestamptz not null default now()
);
create index if not exists page_views_day on public.page_views (day);
alter table public.page_views enable row level security;
drop policy if exists "anyone can count a visit" on public.page_views;
create policy "anyone can count a visit" on public.page_views for insert to anon, authenticated with check (true);
create or replace view public.visits_per_day with (security_invoker = true) as
  select day,
         count(*) filter (where event = 'visit') as visits,
         count(*) filter (where event = 'download-mac') as mac_downloads,
         count(*) filter (where event = 'download-win') as windows_downloads
  from public.page_views group by day order by day desc;
create or replace view public.visits_by_source with (security_invoker = true) as
  select coalesce(nullif(source, ''), 'direct') as came_from,
         count(*) filter (where event = 'visit') as visits,
         count(*) filter (where event <> 'visit') as downloads
  from public.page_views group by 1 order by visits desc;
notify pgrst, 'reload schema';
