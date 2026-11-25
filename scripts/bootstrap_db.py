"""Create local database roles/schema and apply infra/sql migrations.

Usage:
    python scripts/bootstrap_db.py

Idempotent: safe to run after every git pull. Defaults match docker-compose.yml.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

DEFAULT_DATABASE_URL = (
    'postgresql+psycopg://invest_app:ira_dev_9f3c@127.0.0.1:55432/invest'
)
APP_ROLE = 'invest_app'
AUTH_ROLE = 'invest_auth'
SQL_DIR = ROOT / 'infra' / 'sql'


def _app_url():
    # app.config loads .env into os.environ as a side effect.
    from app.config import get_settings

    get_settings()
    return make_url(os.getenv('DATABASE_URL', DEFAULT_DATABASE_URL))


def _conninfo(url) -> dict:
    return {
        'host': url.host or '127.0.0.1',
        'port': url.port or 5432,
        'user': url.username,
        'password': url.password,
        'dbname': url.database,
    }


def _admin_conninfo(app_url) -> dict:
    raw = os.getenv('PG_ADMIN_URL', '').strip()
    if raw:
        return _conninfo(make_url(raw))
    return {
        'host': app_url.host or '127.0.0.1',
        'port': app_url.port or 5432,
        'user': os.getenv('PG_ADMIN_USER', 'invest_admin'),
        'password': (
            os.getenv('PG_ADMIN_PASSWORD')
            or os.getenv('POSTGRES_ADMIN_PASSWORD')
            or 'invest_admin_dev'
        ),
        'dbname': os.getenv('PG_ADMIN_DB', 'postgres'),
    }


def _connect_with_retry(conninfo: dict, label: str, seconds: int = 60):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return psycopg.connect(**conninfo, connect_timeout=3)
        except psycopg.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f'无法连接{label} {conninfo["host"]}:{conninfo["port"]}：{exc}\n'
                    '请确认 PostgreSQL 已启动。使用 Docker 时先执行 docker compose up -d。'
                ) from exc
            print(f'等待{label}启动 …')
            time.sleep(1)


def _ensure_roles(admin, app_password):
    if not app_password:
        raise SystemExit('DATABASE_URL 里没有密码；请为 invest_app 设置连接密码。')
    with admin.cursor() as cur:
        cur.execute('select 1 from pg_roles where rolname = %s', (AUTH_ROLE,))
        if cur.fetchone() is None:
            cur.execute(
                sql.SQL('create role {} nologin bypassrls').format(
                    sql.Identifier(AUTH_ROLE)
                )
            )
        else:
            cur.execute(
                sql.SQL('alter role {} nologin bypassrls').format(
                    sql.Identifier(AUTH_ROLE)
                )
            )

        cur.execute('select 1 from pg_roles where rolname = %s', (APP_ROLE,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL('create role {} login').format(sql.Identifier(APP_ROLE)))
        cur.execute(
            sql.SQL('alter role {} with login password {}').format(
                sql.Identifier(APP_ROLE), sql.Literal(app_password)
            )
        )

        # Temporary membership is only needed while 004 assigns function ownership.
        cur.execute(
            sql.SQL('grant {} to {}').format(
                sql.Identifier(AUTH_ROLE), sql.Identifier(APP_ROLE)
            )
        )
    admin.commit()


def _ensure_database(admin, dbname):
    with admin.cursor() as cur:
        cur.execute('select 1 from pg_database where datname = %s', (dbname,))
        if cur.fetchone() is None:
            cur.execute(
                sql.SQL(
                    "create database {} owner {} template template0 "
                    "encoding 'UTF8' lc_collate 'C' lc_ctype 'C'"
                ).format(
                    sql.Identifier(dbname), sql.Identifier(APP_ROLE)
                )
            )
    admin.commit()




def _migration_files():
    files = sorted(SQL_DIR.glob('[0-9][0-9][0-9]_*.sql'))
    if not files:
        raise SystemExit(f'没有找到迁移脚本：{SQL_DIR}')
    return files

def _strip_transaction(text: str) -> str:
    text = re.sub(r'(?im)^\s*begin\s*;\s*$', '', text, count=1)
    text = re.sub(r'(?im)^\s*commit\s*;\s*$', '', text)
    return text

def _apply_migrations(conn):
    files = _migration_files()
    conn.execute(
        "create table if not exists schema_migrations ("
        "filename text primary key, applied_at timestamptz not null default now())"
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("select filename from schema_migrations")}
    has_tenants = conn.execute(
        "select to_regclass('public.tenants') is not null"
    ).fetchone()[0]
    conn.commit()
    if not applied and has_tenants:
        missing = [
            name for name in ("chat_runs", "session_events")
            if not conn.execute(
                "select to_regclass(%s) is not null", (f"public.{name}",)
            ).fetchone()[0]
        ]
        has_channel = conn.execute(
            "select 1 from information_schema.columns where table_schema='public' "
            "and table_name='evidence' and column_name='channel'"
        ).fetchone()
        if missing or not has_channel:
            raise SystemExit(
                "检测到已有库结构不完整（缺少 006/007 的对象），请先重建数据库。"
            )
        for path in files:
            conn.execute(
                "insert into schema_migrations (filename) values (%s) "
                "on conflict do nothing",
                (path.name,),
            )
        conn.commit()
        print(f"  检测到已有库，登记 {len(files)} 个迁移为已应用")
        return
    for index, path in enumerate(files, 1):
        if path.name in applied:
            print(f"  [{index}/{len(files)}] {path.name} 已应用，跳过")
            continue
        try:
            with conn.transaction():
                conn.execute(_strip_transaction(path.read_text(encoding="utf-8")))
                conn.execute(
                    "insert into schema_migrations (filename) values (%s)",
                    (path.name,),
                )
        except Exception as exc:
            raise SystemExit(f"迁移失败：{path.name}\n{exc}") from exc
        print(f"  [{index}/{len(files)}] {path.name} ok")




def _try_connect(conninfo: dict, seconds: int = 3):
    try:
        return psycopg.connect(**conninfo, connect_timeout=seconds)
    except psycopg.OperationalError:
        return None


def _schema_complete(conn) -> bool:
    names = {
        row[0]
        for row in conn.execute(
            "select tablename from pg_tables where schemaname = 'public'"
        )
    }
    required = {'tenants', 'messages', 'evidence', 'chat_runs', 'session_events'}
    if not required <= names:
        return False
    has_channel = conn.execute(
        "select 1 from information_schema.columns where table_schema = 'public' "
        "and table_name = 'evidence' and column_name = 'channel'"
    ).fetchone()
    has_state = conn.execute(
        "select 1 from information_schema.columns where table_schema = 'public' "
        "and table_name = 'conversations' and column_name = 'agent_state'"
    ).fetchone()
    return bool(has_channel and has_state)


def _verify(conninfo: dict, label: str = '[4/4] 校验') -> None:
    print(label)
    with psycopg.connect(**conninfo) as check:
        tables = check.execute(
            "select count(*) from pg_tables where schemaname = 'public' "
            "and tablename <> 'schema_migrations'"
        ).fetchone()[0]
        forced = check.execute(
            "select relrowsecurity and relforcerowsecurity "
            "from pg_class where oid = 'public.evidence'::regclass"
        ).fetchone()[0]
        owners = check.execute(
            'select count(*) from pg_proc p join pg_roles r on r.oid = p.proowner '
            "where left(p.proname, 5) = 'auth_' and r.rolname = %s",
            (AUTH_ROLE,),
        ).fetchone()[0]
        can_set_role = check.execute(
            "select pg_has_role(current_user, %s, 'MEMBER')", (AUTH_ROLE,)
        ).fetchone()[0]

    print(f'      业务表：{tables} 张')
    print(f'      evidence RLS（强制）：{bool(forced)}')
    print(f'      auth_* 函数归 {AUTH_ROLE} 所有：{owners} 个')
    print(f'      应用账号可切换到 {AUTH_ROLE}：{bool(can_set_role)}')
    if not forced or owners < 6 or can_set_role:
        raise SystemExit('初始化校验未通过，请检查上面的输出。')


def _print_done() -> None:
    print('完成。可启动服务：')
    print('  .\\.venv\\Scripts\\python.exe -m uvicorn app.api_main:app --host 127.0.0.1 --port 8000')


def main() -> int:
    app_url = _app_url()
    dbname = app_url.database or ''
    if dbname in {'', 'postgres', 'template0', 'template1'}:
        raise SystemExit(f'DATABASE_URL 指向了无效数据库：{dbname!r}')

    target_info = _conninfo(app_url)
    target_info['dbname'] = dbname

    # Existing installs can refresh the schema without the admin password.
    existing = _try_connect(target_info)
    if existing is not None:
        complete = _schema_complete(existing)
        existing.close()
        if complete:
            print('[1/3] 已有数据库结构完整，跳过建角色/建库')
            print('[2/3] 执行迁移')
            conn = _connect_with_retry(target_info, '应用数据库')
            try:
                _apply_migrations(conn)
            finally:
                conn.close()
            _verify(target_info, '[3/3] 校验')
            _print_done()
            return 0

    admin_info = _admin_conninfo(app_url)
    print(f'[1/4] 连接管理员 {admin_info["user"]}@{admin_info["host"]}:{admin_info["port"]}')
    admin = _connect_with_retry(admin_info, '管理员数据库')
    admin.autocommit = True
    try:
        print(f'[2/4] 准备角色 {AUTH_ROLE} / {APP_ROLE} 与数据库 {dbname}')
        _ensure_roles(admin, app_url.password)
        _ensure_database(admin, dbname)
    finally:
        admin.close()

    owner_info = dict(target_info, user=admin_info['user'], password=admin_info['password'])

    with psycopg.connect(**owner_info) as owner:
        with owner.cursor() as cur:
            cur.execute(
                sql.SQL('grant create on schema public to {}').format(
                    sql.Identifier(AUTH_ROLE)
                )
            )
        owner.commit()

    print('[3/4] 执行迁移')
    app_conn = _connect_with_retry(target_info, '应用数据库')
    try:
        _apply_migrations(app_conn)
    finally:
        app_conn.close()

    with psycopg.connect(**owner_info) as owner:
        with owner.cursor() as cur:
            cur.execute(
                sql.SQL('revoke create on schema public from {}').format(
                    sql.Identifier(AUTH_ROLE)
                )
            )
            cur.execute(
                sql.SQL('revoke {} from {}').format(
                    sql.Identifier(AUTH_ROLE), sql.Identifier(APP_ROLE)
                )
            )
        owner.commit()

    _verify(target_info)
    _print_done()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())