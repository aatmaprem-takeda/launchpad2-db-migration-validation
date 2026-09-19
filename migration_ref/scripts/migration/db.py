"""Source (legacy SQL Server) and target (PostgreSQL) engine factories.

**Target** reuses the app's configured PostgreSQL engine (``app.db.session.engine``)
so the migration writes to the same database the API reads.

**Source** is the legacy Azure SQL DB. Configure it via environment variables —
either a full SQLAlchemy URL, or discrete parts:

    LEP_SRC_URL   = "mssql+pyodbc://...?driver=ODBC+Driver+18+for+SQL+Server"
      -- or --
    LEP_SRC_SERVER   = sql-lep-data-dev.database.windows.net
    LEP_SRC_DB       = sqldb-lep-data-dev
    LEP_SRC_USER     = LE-DBA-DEV
    LEP_SRC_PASSWORD = ********
    LEP_SRC_DRIVER   = ODBC Driver 18 for SQL Server   (default)
    LEP_SRC_AUTH     = sql | aad                        (default sql)

Azure SQL requires TLS; ``Encrypt=yes`` is always set. For Azure AD accounts set
``LEP_SRC_AUTH=aad`` (uses ``Authentication=ActiveDirectoryPassword``).
"""
from __future__ import annotations

import os
from urllib.parse import quote_plus

from sqlalchemy import Engine, create_engine

# Source and target are SEPARATE Lakebase databases (source = lep2-dev/production
# with the DMS-loaded legacy `lep`+`dbo` schemas; target = launchpad-test/test
# with the app `lep` schema). Because they are different databases/connections,
# both can use the `lep` schema with no collision — so the loaders read
# `lep.<LegacyTable>` from the source and write `lep.<app_table>` to the target
# with no schema rename. (No LEGACY_SCHEMA indirection needed.)


def target_engine() -> Engine:
    """The app's PostgreSQL engine (raises if PG settings are absent)."""
    from app.db.session import engine

    if engine is None:
        raise RuntimeError(
            "Target PostgreSQL is not configured (app.core.config.settings). "
            "Set postgres_host/db/user/password or database_url."
        )
    return engine


def source_url() -> str:
    """Build the SQLAlchemy URL for the legacy SQL Server source from env."""
    if os.environ.get("LEP_SRC_URL"):
        return os.environ["LEP_SRC_URL"]

    server = os.environ.get("LEP_SRC_SERVER", "sql-lep-data-dev.database.windows.net")
    database = os.environ.get("LEP_SRC_DB", "sqldb-lep-data-dev")
    user = os.environ.get("LEP_SRC_USER")
    password = os.environ.get("LEP_SRC_PASSWORD")
    driver = os.environ.get("LEP_SRC_DRIVER", "ODBC Driver 18 for SQL Server")
    auth = os.environ.get("LEP_SRC_AUTH", "sql").lower()

    if not user or not password:
        raise RuntimeError(
            "Set LEP_SRC_USER and LEP_SRC_PASSWORD (or a full LEP_SRC_URL) for the "
            "legacy SQL Server source."
        )

    odbc = (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        f"UID={user};PWD={password};Encrypt=yes;TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    if auth == "aad":
        odbc += "Authentication=ActiveDirectoryPassword;"
    return "mssql+pyodbc:///?odbc_connect=" + quote_plus(odbc)


def source_engine() -> Engine:
    """Read-only-intent engine over the legacy SQL Server."""
    # fast_executemany is irrelevant for reads; pool_pre_ping guards dropped conns.
    return create_engine(source_url(), pool_pre_ping=True)
