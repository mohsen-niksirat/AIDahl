-- Phase 6: storage hygiene + lifetime quota
-- Run AFTER 03_phase5.sql

alter table public.user_quotas
  add column if not exists lifetime_limit int not null default 3000000;

alter table public.user_quotas
  add column if not exists lifetime_used int not null default 0;

alter table public.user_quotas
  add column if not exists lifetime_warned_at timestamptz;

comment on column public.user_quotas.lifetime_limit is
  'Total tokens a user may spend over account lifetime (bot gateway accounting)';
comment on column public.user_quotas.lifetime_used is
  'Accumulated tokens counted by the bot (usage rows may be pruned)';
