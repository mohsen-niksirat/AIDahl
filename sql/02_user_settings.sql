-- Phase 4: per-user settings on existing users table
-- Run in Supabase SQL Editor after 01_user_api_keys.sql

alter table public.users
  add column if not exists response_style text not null default 'html';

alter table public.users
  add column if not exists context_messages int;

comment on column public.users.response_style is
  'plain | markdown | html — how bot delivers LLM replies';
comment on column public.users.context_messages is
  'Optional per-user history size; null = server HISTORY_LIMIT';
