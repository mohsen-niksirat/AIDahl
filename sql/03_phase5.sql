-- Phase 5: streaming support data, quotas, multi-chat, admin flags
-- Run in Supabase SQL Editor AFTER 01 and 02

-- Pointer to the conversation the bot currently uses
alter table public.users
  add column if not exists active_conversation_id uuid;

alter table public.users
  add column if not exists is_admin boolean not null default false;

alter table public.users
  add column if not exists stream_enabled boolean;

-- Daily token quota (fast path). Actual spend also logged in public.usage.
create table if not exists public.user_quotas (
  user_id bigint primary key,
  daily_limit int not null default 50000,
  used_today int not null default 0,
  reset_at date not null default current_date,
  updated_at timestamptz not null default now()
);

alter table public.user_quotas enable row level security;
-- Backend-only (same pattern as other bot tables): disable RLS for bot key access
alter table public.user_quotas disable row level security;

create index if not exists conversations_user_updated_idx
  on public.conversations (user_id, updated_at desc);

create index if not exists usage_created_at_idx
  on public.usage (created_at);

comment on table public.user_quotas is
  'Per-user daily token quota for the bot gateway (shared key or all keys per env)';
