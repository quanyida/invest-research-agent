begin;

-- 把 auth_* 函数的所有者交给 invest_auth（NOLOGIN + BYPASSRLS）。
-- 用循环而不是逐条 ALTER：初始化脚本可以重复执行，
-- 已属于 invest_auth 的函数会被跳过，不再要求当前连接是函数所有者。
do $$
declare
  fn record;
begin
  for fn in
    select p.oid::regprocedure as signature
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    join pg_roles r on r.oid = p.proowner
    where n.nspname = 'public'
      and p.proname in (
        'auth_register',
        'auth_login_lookup',
        'auth_session_create',
        'auth_session_resolve',
        'auth_session_revoke',
        'auth_session_touch'
      )
      and r.rolname <> 'invest_auth'
  loop
    execute format('alter function %s owner to invest_auth', fn.signature);
  end loop;
end;
$$;

grant execute on function auth_register(uuid, uuid, text, text, text) to invest_app;
grant execute on function auth_login_lookup(text) to invest_app;
grant execute on function auth_session_create(uuid, text, uuid, uuid, timestamptz) to invest_app;
grant execute on function auth_session_resolve(text) to invest_app;
grant execute on function auth_session_revoke(text) to invest_app;
grant execute on function auth_session_touch(text) to invest_app;

commit;