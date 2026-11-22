begin;

create or replace function auth_register(
  p_tenant_id uuid,
  p_user_id uuid,
  p_email text,
  p_password_hash text,
  p_tenant_name text
) returns table (tenant_id uuid, user_id uuid)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into tenants (id, name) values (p_tenant_id, p_tenant_name);
  insert into users (id, tenant_id, email, password_hash, role)
    values (p_user_id, p_tenant_id, lower(p_email), p_password_hash, 'owner');
  return query select p_tenant_id, p_user_id;
end;
$$;

create or replace function auth_login_lookup(p_email text)
returns table (
  user_id uuid,
  tenant_id uuid,
  email text,
  password_hash text,
  role text,
  tenant_name text,
  tenant_status text
)
language sql
security definer
set search_path = public, pg_temp
as $$
  select u.id, u.tenant_id, u.email, u.password_hash, u.role, t.name, t.status
  from users u join tenants t on t.id = u.tenant_id
  where lower(u.email) = lower(p_email)
  limit 1;
$$;

create or replace function auth_session_create(
  p_session_id uuid,
  p_token_hash text,
  p_user_id uuid,
  p_tenant_id uuid,
  p_expires_at timestamptz
) returns void
language sql
security definer
set search_path = public, pg_temp
as $$
  insert into sessions (id, token_hash, user_id, tenant_id, expires_at)
  values (p_session_id, p_token_hash, p_user_id, p_tenant_id, p_expires_at);
$$;

create or replace function auth_session_resolve(p_token_hash text)
returns table (
  session_id uuid,
  user_id uuid,
  tenant_id uuid,
  email text,
  role text,
  tenant_name text,
  expires_at timestamptz
)
language sql
security definer
set search_path = public, pg_temp
as $$
  select s.id, u.id, u.tenant_id, u.email, u.role, t.name, s.expires_at
  from sessions s
  join users u on u.id = s.user_id and u.tenant_id = s.tenant_id
  join tenants t on t.id = u.tenant_id
  where s.token_hash = p_token_hash
    and s.revoked_at is null
    and s.expires_at > now()
    and t.status = 'active'
  limit 1;
$$;

create or replace function auth_session_revoke(p_token_hash text)
returns void
language sql
security definer
set search_path = public, pg_temp
as $$
  update sessions set revoked_at = now()
  where token_hash = p_token_hash and revoked_at is null;
$$;

create or replace function auth_session_touch(p_token_hash text)
returns void
language sql
security definer
set search_path = public, pg_temp
as $$
  update sessions set last_seen_at = now()
  where token_hash = p_token_hash and revoked_at is null and expires_at > now();
$$;

revoke all on function auth_register(uuid, uuid, text, text, text) from public;
revoke all on function auth_login_lookup(text) from public;
revoke all on function auth_session_create(uuid, text, uuid, uuid, timestamptz) from public;
revoke all on function auth_session_resolve(text) from public;
revoke all on function auth_session_revoke(text) from public;
revoke all on function auth_session_touch(text) from public;

grant execute on function auth_register(uuid, uuid, text, text, text) to invest_app;
grant execute on function auth_login_lookup(text) to invest_app;
grant execute on function auth_session_create(uuid, text, uuid, uuid, timestamptz) to invest_app;
grant execute on function auth_session_resolve(text) to invest_app;
grant execute on function auth_session_revoke(text) to invest_app;
grant execute on function auth_session_touch(text) to invest_app;

grant select, insert, update, delete on all tables in schema public to invest_auth;

commit;