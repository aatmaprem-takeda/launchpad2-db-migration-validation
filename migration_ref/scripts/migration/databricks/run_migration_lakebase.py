# Databricks notebook source
# MAGIC %md
# MAGIC # LEP migration — Lakebase (legacy) → Lakebase (app), 3 brands
# MAGIC
# MAGIC Migrates the transactional data for the migration brands from the
# MAGIC **source** Lakebase branch (DMS-loaded legacy, schema `lep`) into the
# MAGIC **target** Lakebase branch (the new app, schema `lep`).
# MAGIC
# MAGIC | | Lakebase | schema | role |
# MAGIC |---|---|---|---|
# MAGIC | **Source** | `lep2-dev` / `production` | `lep` (Projects, Tasks, …), `dbo` | read legacy |
# MAGIC | **Target** | `launchpad-test` / `test` | `lep` (launch_plans, …) | write app |
# MAGIC
# MAGIC This notebook **reuses the tested Python ETL** in `scripts/migration/`
# MAGIC (same crosswalks, scope filter, FK-safe order) — it only points the two
# MAGIC engines at the two Lakebase branches and runs the loaders. Default scope is
# MAGIC the 3 migration brands (`scope.py`); set the scope widget to `full` for all.
# MAGIC
# MAGIC **Prerequisites**
# MAGIC 1. The repo is available in the workspace (Databricks Repos) — set `repo_root`.
# MAGIC 2. The **target** branch is already seeded (Alembic + reference seeds:
# MAGIC    business_units, countries, brand_molecules incl. the 3 brands, roles).
# MAGIC 3. Passwords: entered via widgets (no secret scope needed). For production,
# MAGIC    upgrade to a Databricks **secret scope** (see the note in the config cell).

# COMMAND ----------
# MAGIC %pip install "sqlalchemy>=2" "psycopg[binary]"

# COMMAND ----------
# MAGIC %md ## 1. Config (widgets + secrets)

# COMMAND ----------
dbutils.widgets.text("repo_root", "/Workspace/Repos/<you>/apms-91125-admin-launch-excellence-platform-api")

# Source = lep2-dev / production (DMS-loaded legacy, schema lep)
dbutils.widgets.text("src_host", "")
dbutils.widgets.text("src_db", "lep2-dev")
dbutils.widgets.text("src_user", "")
dbutils.widgets.text("src_port", "5432")
dbutils.widgets.text("src_password", "")   # NOTE: visible in the widget bar

# Target = launchpad-test / test-branch (the app DB, schema lep)
dbutils.widgets.text("tgt_host", "")
dbutils.widgets.text("tgt_db", "launchpad-test")
dbutils.widgets.text("tgt_user", "")
dbutils.widgets.text("tgt_port", "5432")
dbutils.widgets.text("tgt_password", "")   # NOTE: visible in the widget bar

dbutils.widgets.dropdown("scope", "brands", ["brands", "full"])
dbutils.widgets.dropdown("mode", "dry-run", ["dry-run", "load"])

# Passwords come straight from the widgets (no secret scope needed to get started).
# SECURITY: widget values are visible — do NOT commit this notebook with them
# filled, and clear the widgets when done. To upgrade later, create a secret scope
# and swap the two lines below for: dbutils.secrets.get(scope="lep-migration", key="src_password")
def _pw(key: str) -> str:
    val = dbutils.widgets.get(key)
    if not val:
        raise ValueError(f"widget '{key}' is empty — enter the password")
    return val

def _pg_url(host, port, db, user, pw_key):
    from urllib.parse import quote_plus
    return (f"postgresql+psycopg://{user}:{quote_plus(_pw(pw_key))}"
            f"@{host}:{port}/{db}?sslmode=require")

SRC_URL = _pg_url(dbutils.widgets.get("src_host"), dbutils.widgets.get("src_port"),
                  dbutils.widgets.get("src_db"), dbutils.widgets.get("src_user"), "src_password")
TGT_URL = _pg_url(dbutils.widgets.get("tgt_host"), dbutils.widgets.get("tgt_port"),
                  dbutils.widgets.get("tgt_db"), dbutils.widgets.get("tgt_user"), "tgt_password")

# scope.py reads LEP_MIGRATION_SCOPE at call time; 'brands' (default) or 'full'
import os
os.environ["LEP_MIGRATION_SCOPE"] = "full" if dbutils.widgets.get("scope") == "full" else "brands"

# make the repo importable
import sys
repo_root = dbutils.widgets.get("repo_root")
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

# COMMAND ----------
# MAGIC %md ## 2. Build the two Lakebase engines

# COMMAND ----------
from sqlalchemy import create_engine, text

src = create_engine(SRC_URL, pool_pre_ping=True)
tgt = create_engine(TGT_URL, pool_pre_ping=True)

with src.connect() as c:
    print("source ok:", c.execute(text("select current_database()")).scalar())
with tgt.connect() as c:
    print("target ok:", c.execute(text("select current_database()")).scalar())

# COMMAND ----------
# MAGIC %md ## 3. Pre-flight checks — table case + target seed state
# MAGIC The loaders read unquoted `lep.Projects`, which Postgres folds to
# MAGIC `lep.projects`. If DMS created **case-preserved** tables (`"Projects"`),
# MAGIC the reads won't match — this cell flags that before we load.

# COMMAND ----------
with src.connect() as c:
    tabs = [r[0] for r in c.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'lep' ORDER BY table_name"))]
print("source lep tables (first 20):", tabs[:20])
case_preserved = any(t != t.lower() for t in tabs)
print("\n>>> CASE-PRESERVED TABLE NAMES:", case_preserved,
      "\n    (if True, the loaders need quoted identifiers — tell the maintainer before loading)")

with tgt.connect() as c:
    for tbl in ("business_units", "countries", "brand_molecules", "roles", "launch_frameworks"):
        n = c.execute(text(f"SELECT count(*) FROM lep.{tbl}")).scalar()
        print(f"target lep.{tbl}: {n}")
print("\n>>> target must be SEEDED (non-zero above) or plans won't resolve")

# COMMAND ----------
# MAGIC %md ## 4. Run the migration (reuses the tested ETL, FK-safe order)
# MAGIC `mode=dry-run` → counts only, no writes. `mode=load` → writes + audit.

# COMMAND ----------
from scripts.migration.run_all import _plan
from scripts.migration.audit import MigrationAudit, timed
from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.scope import scope_label

dry = dbutils.widgets.get("mode") == "dry-run"
print(f"== scope: {scope_label()} ==  mode: {'DRY-RUN' if dry else 'LOAD'}")

print("== resolution maps ==")
maps = ResolutionMaps.load(src)
print(f"[resolution_maps] {maps.summary()}")

audit = None if dry else MigrationAudit(tgt)
if audit:
    print(f"== audit run_id: {audit.run_id} ==")

failed = False
for name, tbl, fn in _plan(src, tgt, maps, dry):
    print(f"== {name} ==")
    result, ms, err = timed(fn)
    print(f"   {name}: {result if not err else 'ERROR: ' + str(err)[:300]}  ({ms} ms)")
    if audit:
        audit.stage(name, tbl, None if err else result,
                    "error" if err else "ok",
                    message=str(err)[:500] if err else None, duration_ms=ms)
    if err:
        failed = True
        break  # FK-safe: stop on first failure

if audit:
    audit.run("error" if failed else "ok")
    print("[audit] wrote lep.migration_audit;", audit.seq, "rows")
print("FAILED." if failed else ("Dry run complete." if dry else "Done."))

# COMMAND ----------
# MAGIC %md ## 5. Verify (target counts)

# COMMAND ----------
with tgt.connect() as c:
    for tbl in ("launch_plans", "workplan_items", "workplan_item_assignments",
                "plan_memberships", "trackers"):
        n = c.execute(text(f"SELECT count(*) FROM lep.{tbl}")).scalar()
        print(f"target lep.{tbl}: {n}")
