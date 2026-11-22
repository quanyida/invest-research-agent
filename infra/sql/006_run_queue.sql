-- C 路线：durable run queue + SSE replay log
-- chat_runs 是内部控制表，不含用户内容，因此不启用 RLS；
-- 所有 API 读取都必须显式带 tenant_id 过滤。
begin;

create table if not exists chat_runs (
  id uuid primary key,
  tenant_id uuid not null,
  user_id uuid not null,
  conversation_id uuid not null,
  message_id uuid not null,
  request_id uuid not null,
  status text not null default 'queued',
  attempt integer not null default 0,
  worker_id text,
  lease_until timestamptz,
  error_code text,
  error_message text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  constraint chat_runs_conversation_fk
    foreign key (tenant_id, conversation_id) references conversations (tenant_id, id),
  constraint chat_runs_message_fk
    foreign key (tenant_id, message_id) references messages (tenant_id, id)
);

-- 同一会话最多一个 queued/running，靠结构而不是靠应用层判断
create unique index if not exists chat_runs_one_active
  on chat_runs (conversation_id)
  where status in ('queued', 'running');

create index if not exists chat_runs_pick_idx
  on chat_runs (status, created_at);

create index if not exists chat_runs_lease_idx
  on chat_runs (lease_until)
  where status = 'running';

create index if not exists chat_runs_message_idx
  on chat_runs (message_id);

create table if not exists session_events (
  id bigserial primary key,
  tenant_id uuid not null,
  user_id uuid not null,
  conversation_id uuid not null,
  message_id uuid not null,
  event text not null,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists session_events_message_idx
  on session_events (message_id, id);

alter table session_events enable row level security;
alter table session_events force row level security;

drop policy if exists session_events_owner on session_events;
create policy session_events_owner on session_events
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid);

commit;