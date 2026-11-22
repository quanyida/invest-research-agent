begin;

alter table tenants enable row level security;
alter table users enable row level security;
alter table sessions enable row level security;
alter table conversations enable row level security;
alter table messages enable row level security;
alter table evidence enable row level security;
alter table tool_calls enable row level security;
alter table llm_calls enable row level security;
alter table feedback enable row level security;
alter table usage_daily enable row level security;

alter table tenants force row level security;
alter table users force row level security;
alter table sessions force row level security;
alter table conversations force row level security;
alter table messages force row level security;
alter table evidence force row level security;
alter table tool_calls force row level security;
alter table llm_calls force row level security;
alter table feedback force row level security;
alter table usage_daily force row level security;

drop policy if exists tenants_self on tenants;
create policy tenants_self on tenants
  using (id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check (id = nullif(current_setting('app.tenant_id', true), '')::uuid);

drop policy if exists users_self on users;
create policy users_self on users
  using (id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (id = nullif(current_setting('app.user_id', true), '')::uuid);

drop policy if exists sessions_token on sessions;
create policy sessions_token on sessions
  using (token_hash = nullif(current_setting('app.session_hash', true), ''))
  with check (token_hash = nullif(current_setting('app.session_hash', true), ''));

drop policy if exists conversations_owner on conversations;
create policy conversations_owner on conversations
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid);

drop policy if exists messages_owner on messages;
create policy messages_owner on messages
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid);

drop policy if exists evidence_owner on evidence;
create policy evidence_owner on evidence
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid);

drop policy if exists tool_calls_owner on tool_calls;
create policy tool_calls_owner on tool_calls
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid);

drop policy if exists llm_calls_owner on llm_calls;
create policy llm_calls_owner on llm_calls
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid);

drop policy if exists feedback_owner on feedback;
create policy feedback_owner on feedback
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid);

drop policy if exists usage_daily_owner on usage_daily;
create policy usage_daily_owner on usage_daily
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
     and user_id = nullif(current_setting('app.user_id', true), '')::uuid);

commit;