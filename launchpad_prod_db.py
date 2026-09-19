"""Reusable, read-only connector for the LaunchPad 2.0 production database.

Import this from any session instead of rediscovering the connection each time.

    from launchpad_prod_db import connect, describe
    with connect() as conn: ...

The one thing that trips everyone up: the Databricks personal access token is
NOT the Postgres password. Lakebase wants a short-lived OAuth JWT (about one
hour). Supplying the PAT produces:

    "Provided authentication token is not a valid JWT encoding"

Supply the JWT one of these ways, checked in order:

1. Environment variable ``LAUNCHPAD_PROD_DB_OAUTH_TOKEN``.
2. A token file at ``%USERPROFILE%\\.launchpad\\db-oauth-token`` (first line).
3. Minted automatically, if ``LAUNCHPAD_PROD_DATABRICKS_HOST`` and
   ``LAUNCHPAD_PROD_LAKEBASE_ENDPOINT`` are set in the secure env and a valid
   PAT is available. Requires the Databricks CLI on PATH.

Every session opened here is read-only at both the session and transaction
level. Compliance review dated 2026-09-18 approved read-only extraction from
this database and refused writes; see
``docs/reference/launchpad-prod-db-access.md``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import psycopg

SECURE_ENV = Path(os.path.expandvars(r"%USERPROFILE%\.launchpad\launchpad-prod.env"))
TOKEN_FILE = Path(os.path.expandvars(r"%USERPROFILE%\.launchpad\db-oauth-token"))


class ProdDbCredentialError(RuntimeError):
    """No usable Lakebase OAuth token was found, and none could be minted."""


class ProdDbConfigError(RuntimeError):
    """The secure env file is missing or incomplete."""


def load_secure_env(path: Path = SECURE_ENV) -> dict[str, str]:
    if not path.exists():
        raise ProdDbConfigError(
            f"Secure env not found at {path}. See docs/reference/launchpad-prod-db-access.md"
        )
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _mint_token(env: dict[str, str]) -> str | None:
    host = env.get("LAUNCHPAD_PROD_DATABRICKS_HOST", "")
    endpoint = env.get("LAUNCHPAD_PROD_LAKEBASE_ENDPOINT", "")
    pat = env.get("LAUNCHPAD_PROD_DATABRICKS_TOKEN", "")
    if not host or not endpoint or len(pat) < 20 or "REPLACE_WITH" in host:
        return None
    try:
        proc = subprocess.run(
            ["databricks", "postgres", "generate-database-credential", endpoint,
             "--ttl", "3600s", "-o", "json"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "DATABRICKS_HOST": host, "DATABRICKS_TOKEN": pat},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    import json
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return payload.get("token") or payload.get("credential") or None


def get_token(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else load_secure_env()

    token = os.environ.get("LAUNCHPAD_PROD_DB_OAUTH_TOKEN", "").strip()
    if len(token) > 40:
        return token

    if TOKEN_FILE.exists():
        first = TOKEN_FILE.read_text(encoding="utf-8").strip().splitlines()
        if first and len(first[0].strip()) > 40:
            return first[0].strip()

    minted = _mint_token(env)
    if minted:
        return minted

    raise ProdDbCredentialError(
        "No Lakebase OAuth token available. A Databricks PAT will NOT work as the "
        "password. Provide a JWT via LAUNCHPAD_PROD_DB_OAUTH_TOKEN, or write it to "
        f"{TOKEN_FILE}, or configure LAUNCHPAD_PROD_DATABRICKS_HOST and "
        "LAUNCHPAD_PROD_LAKEBASE_ENDPOINT in the secure env so it can be minted. "
        "See docs/reference/launchpad-prod-db-access.md"
    )


def connect(*, read_only: bool = True, connect_timeout: int = 20) -> psycopg.Connection:
    """Open a connection to the production database. Read-only unless told otherwise.

    ``read_only=False`` is deliberately available but is NOT authorised by the
    standing compliance review. Do not use it without a change record.
    """
    env = load_secure_env()
    missing = [
        k for k in ("LAUNCHPAD_PROD_DATABASE_HOST", "LAUNCHPAD_PROD_DATABASE_NAME",
                    "LAUNCHPAD_PROD_DATABASE_USER")
        if not env.get(k)
    ]
    if missing:
        raise ProdDbConfigError(f"Secure env missing keys: {', '.join(missing)}")

    options = "-c default_transaction_read_only=on" if read_only else None
    conn = psycopg.connect(
        host=env["LAUNCHPAD_PROD_DATABASE_HOST"],
        port=int(env.get("LAUNCHPAD_PROD_DATABASE_PORT", "5432")),
        dbname=env["LAUNCHPAD_PROD_DATABASE_NAME"],
        user=env["LAUNCHPAD_PROD_DATABASE_USER"],
        password=get_token(env),
        sslmode=env.get("LAUNCHPAD_PROD_DATABASE_SSLMODE", "require"),
        connect_timeout=connect_timeout,
        options=options,
    )
    if read_only:
        with conn.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            if cur.fetchone()[0] != "on":
                conn.close()
                raise ProdDbConfigError("Read-only guard did not take effect; refusing to proceed.")
    return conn


def describe() -> None:
    """Print a safe, secret-free summary of what is configured and reachable."""
    try:
        env = load_secure_env()
    except ProdDbConfigError as exc:
        print(f"config: {exc}")
        return
    host = env.get("LAUNCHPAD_PROD_DATABASE_HOST", "")
    print(f"host_configured={bool(host)} db={env.get('LAUNCHPAD_PROD_DATABASE_NAME', '?')}")
    print(f"workspace={env.get('LAUNCHPAD_PROD_DATABRICKS_HOST', '(not set)')}")
    print(f"endpoint={env.get('LAUNCHPAD_PROD_LAKEBASE_ENDPOINT', '(not set)')}")
    try:
        token = get_token(env)
        print(f"oauth_token=present length={len(token)}")
    except ProdDbCredentialError as exc:
        print(f"oauth_token=ABSENT\n  {exc}")
        return
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            db, user = cur.fetchone()
            print(f"CONNECTED db={db} user={user} mode=read-only")
    except Exception as exc:  # noqa: BLE001
        print(f"CONNECT FAILED: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")


if __name__ == "__main__":
    describe()
