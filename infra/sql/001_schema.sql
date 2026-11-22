begin;

create table if not exists tenants (
  id uuid primary key,
  name text not null,
  plan text not null default 'free',
  status text not null default 'active',
  created_at timestamptz not null default now()
);

create table if not exists users (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  email text not null,
  password_hash text not null,
  role text not null default 'owner',
  created_at timestamptz not null default now()
);
create unique index if not exists users_email_lower_uidx on users (lower(email));
create unique index if not exists users_tenant_id_uidx on users (tenant_id, id);

create table if not exists sessions (
  id uuid primary key,
  token_hash text not null unique,
  user_id uuid not null,
  tenant_id uuid not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  last_seen_at timestamptz,
  foreign key (tenant_id, user_id) references users(tenant_id, id)
);
create index if not exists sessions_active_lookup on sessions (token_hash) where revoked_at is null;

create table if not exists conversations (
  id uuid primary key,
  tenant_id uuid not null,
  user_id uuid not null,
  title text not null default '新会话',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz,
  unique (tenant_id, id),
  foreign key (tenant_id, user_id) references users(tenant_id, id)
);
create index if not exists conversations_owner_updated_idx on conversations (tenant_id, user_id, updated_at desc);

create table if not exists messages (
  id uuid primary key,
  tenant_id uuid not null,
  user_id uuid not null,
  conversation_id uuid not null,
  request_id uuid not null,
  client_request_id text,
  role text not null,
  content text not null default '',
  status text not null default 'running',
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (tenant_id, id),
  foreign key (tenant_id, user_id) references users(tenant_id, id),
  foreign key (tenant_id, conversation_id) references conversations(tenant_id, id)
);
create index if not exists messages_conversation_created_idx on messages (conversation_id, created_at);
create unique index if not exists messages_client_request_uidx
  on messages (tenant_id, user_id, client_request_id)
  where client_request_id is not null;

create table if not exists evidence (
  id uuid primary key,
  tenant_id uuid not null,
  user_id uuid not null,
  message_id uuid not null,
  seq int not null,
  tool text not null,
  server text not null,
  upstream_tool text not null,
  args jsonb not null default '{}'::jsonb,
  args_hash text not null,
  query_text text,
  period_end date,
  data_as_of timestamptz,
  as_of_label text,
  fetched_at timestamptz not null,
  payload jsonb not null,
  facts jsonb not null default '[]'::jsonb,
  raw_excerpt text,
  truncated boolean not null default false,
  parse_quality numeric(3,2) not null default 0.90 check (parse_quality between 0 and 1),
  parser_version text,
  tool_schema_hash text,
  source_title text,
  source_url text,
  source_publisher text,
  source_published_at timestamptz,
  channel text not null default 'unknown',
  unique (message_id, seq),
  unique (tenant_id, id),
  foreign key (tenant_id, user_id) references users(tenant_id, id),
  foreign key (tenant_id, message_id) references messages(tenant_id, id)
);
create index if not exists evidence_message_seq_idx on evidence (message_id, seq);
create index if not exists evidence_channel_idx on evidence (channel, fetched_at);

create table if not exists tool_calls (
  id uuid primary key,
  tenant_id uuid not null,
  user_id uuid not null,
  message_id uuid not null,
  conversation_id uuid not null,
  request_id uuid not null,
  parent_id uuid,
  step int not null default 1,
  tool text not null,
  server text not null,
  upstream_tool text not null,
  args jsonb not null default '{}'::jsonb,
  args_hash text not null,
  query_text text,
  status text not null,
  cached boolean not null default false,
  latency_ms int not null,
  error_code text,
  created_at timestamptz not null default now(),
  foreign key (tenant_id, user_id) references users(tenant_id, id),
  foreign key (tenant_id, message_id) references messages(tenant_id, id)
);
create index if not exists tool_calls_tenant_created_idx on tool_calls (tenant_id, created_at desc);

create table if not exists llm_calls (
  id uuid primary key,
  tenant_id uuid not null,
  user_id uuid not null,
  message_id uuid not null,
  request_id uuid not null,
  purpose text not null,
  model text not null,
  prompt_version text,
  temperature numeric(3,2),
  max_tokens int,
  prompt_tokens int not null default 0,
  completion_tokens int not null default 0,
  reasoning_tokens int not null default 0,
  finish_reason text,
  latency_ms int not null,
  status text not null,
  created_at timestamptz not null default now(),
  foreign key (tenant_id, user_id) references users(tenant_id, id),
  foreign key (tenant_id, message_id) references messages(tenant_id, id)
);

create table if not exists feedback (
  id uuid primary key,
  tenant_id uuid not null,
  user_id uuid not null,
  message_id uuid not null,
  rating text not null,
  comment text,
  created_at timestamptz not null default now(),
  unique (message_id, user_id),
  foreign key (tenant_id, user_id) references users(tenant_id, id),
  foreign key (tenant_id, message_id) references messages(tenant_id, id)
);

create table if not exists usage_daily (
  tenant_id uuid not null,
  user_id uuid not null,
  day date not null,
  questions int not null default 0,
  tool_calls int not null default 0,
  llm_calls int not null default 0,
  failed int not null default 0,
  prompt_tokens bigint not null default 0,
  completion_tokens bigint not null default 0,
  total_latency_ms bigint not null default 0,
  primary key (tenant_id, user_id, day),
  foreign key (tenant_id, user_id) references users(tenant_id, id)
);

commit;