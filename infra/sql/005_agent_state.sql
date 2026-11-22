alter table conversations
  add column if not exists agent_state jsonb not null default '{}'::jsonb,
  add column if not exists agent_state_version integer not null default 0,
  add column if not exists agent_state_updated_at timestamptz;