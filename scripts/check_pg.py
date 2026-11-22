"""Print the database identity/encoding used by DATABASE_URL."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg
from sqlalchemy.engine import make_url

from app.config import get_settings


def main() -> int:
    url = make_url(get_settings().database_url)
    with psycopg.connect(
        host=url.host or '127.0.0.1',
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        dbname=url.database,
        connect_timeout=5,
    ) as conn:
        row = conn.execute(
            "select current_user, current_database(), "
            "current_setting('server_encoding'), version()"
        ).fetchone()
    print('USER_DB_ENC:', row[0], row[1], row[2])
    print('VERSION:', row[3][:38])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())