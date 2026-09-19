"""Shared, read-only access layer for the LEP -> LaunchPad DB parity check.

Two production databases, both opened read-only:

* Legacy LEP  — Azure SQL (`sql-lep-data-prod` / `sqldb-lep-data-prod`),
  credentials from a local ``.env`` next to this file (``LEP_PROD_*`` keys) or
  from ``%USERPROFILE%\\.launchpad\\launchpad-prod.env``. Opened through
  SQLAlchemy ``mssql+pymssql`` so the migration's own ``ResolutionMaps`` can be
  reused verbatim.
* LaunchPad 2.0 — Lakebase Postgres, via ``launchpad_prod_db.py`` in this repo
  (enforces ``default_transaction_read_only=on`` and verifies it took).

The migration code that actually ran in production (origin/main @ 26166fc,
run-20260919T030419Z) is vendored under ``migration_ref/`` and imported here so
every transform, scope rule and deterministic ID in the comparison is the
migration's own, not a re-implementation.

Guard: ``q()`` / ``qpg()`` refuse any statement that is not a single SELECT.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

HERE = Path(__file__).resolve().parent

# migration reference code (the version that ran in prod) + local connector
sys.path.insert(0, str(HERE / "migration_ref"))
sys.path.insert(0, str(HERE))

_WRITE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|COPY|GRANT|REVOKE|EXEC)\b",
    re.IGNORECASE,
)


def assert_select_only(sql: str) -> str:
    if _WRITE_RE.search(sql):
        raise PermissionError(f"REFUSED non-SELECT statement: {sql[:120]}")
    return sql


def _env_files() -> list[Path]:
    return [
        HERE / ".env",
        Path(os.path.expandvars(r"%USERPROFILE%\.launchpad\launchpad-prod.env")),
    ]


def _load_env() -> dict[str, str]:
    """Merge key=value pairs from the local .env and the user-profile secure env."""
    env: dict[str, str] = {}
    for path in _env_files():
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def legacy_engine():
    """SQLAlchemy engine over the legacy LEP production Azure SQL (read intent)."""
    from sqlalchemy import create_engine

    e = _load_env()
    missing = [k for k in ("LEP_PROD_USER", "LEP_PROD_PASSWORD", "LEP_PROD_SERVER", "LEP_PROD_DB") if not e.get(k)]
    if missing:
        raise RuntimeError(
            f"Legacy LEP credentials missing: {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in (see README)."
        )
    url = (
        f"mssql+pymssql://{quote_plus(e['LEP_PROD_USER'])}:{quote_plus(e['LEP_PROD_PASSWORD'])}"
        f"@{e['LEP_PROD_SERVER']}/{e['LEP_PROD_DB']}?charset=UTF-8"
    )
    return create_engine(url, pool_pre_ping=True)


def launchpad_conn():
    """psycopg connection to LaunchPad prod Lakebase, session read-only enforced."""
    from launchpad_prod_db import connect

    return connect()  # read_only=True is the default and verified in-session


def q(engine, sql: str, **params):
    """Run a SELECT on the legacy engine, return list of dict rows."""
    from sqlalchemy import text

    assert_select_only(sql)
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


def qpg(conn, sql: str, params=None):
    """Run a SELECT on the LaunchPad connection, return list of tuples + colnames."""
    assert_select_only(sql)
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d.name for d in cur.description] if cur.description else []
        return cols, cur.fetchall()


def qpg_dicts(conn, sql: str, params=None) -> list[dict]:
    cols, rows = qpg(conn, sql, params)
    return [dict(zip(cols, r)) for r in rows]
