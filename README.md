# LaunchPad 2.0 — production DB migration validation

Verifies that every plan in the LaunchPad 2.0 production database (Lakebase
Postgres) was migrated correctly from the legacy LEP production database
(Azure SQL), accounting for the migration's designed transformations.

Both databases are opened **strictly read-only**. Every query path goes through
a guard that refuses anything except a single `SELECT`, and the Lakebase session
additionally sets and verifies `default_transaction_read_only=on`.

## How it works

The comparison does not re-invent the migration rules. The exact migration code
that ran in production (`origin/main @ 26166fc`, latest run
`run-20260919T120011Z`) is
vendored under `migration_ref/` and imported directly, so every transform,
scope rule and deterministic uuid5 ID used in the comparison is the migration's
own. The scripts rebuild the *expected* LaunchPad state from live legacy data
through that code, then diff it against the *actual* LaunchPad state.

Every difference is classified, in this order:

| Class | Meaning |
|---|---|
| `EXPLAINED:<rule>` | A designed transformation (WBS renumbering, injection milestones, the cutover banner posted into legacy). |
| `DRIFT` | Either side was edited **after** the migration ran — normal app activity, not a migration issue. |
| `STALE_SOURCE` | Legacy was edited after the migration source's replication position (the DMS replica syncs continuously with a small lag; `SNAPSHOT_CUTOFF` in `phase2_compare.py` records that position). Data faithfully copied; the edit simply arrived later. |
| `GAP` | None of the above — a real, unexplained migration defect. **This is the number that must be zero.** |

The 2026-09-19 validation of re-migration `run-20260919T120011Z` (executed
from a fresh, continuously-syncing DMS replica) found **complete parity**:
zero missing rows on every entity and **0 GAPs** across 87 plans, ~50k tasks
and ~130k child rows. See `PROMPT.md` to reproduce or extend it.

## Prerequisites

1. **Python 3.11+** with the packages in `requirements.txt`
   (`pip install -r requirements.txt`). On Windows, `pymssql` installs as a
   prebuilt wheel — no compiler needed.
2. **Databricks CLI** on PATH (`databricks --version`), authenticated to the
   production workspace, if you want the Lakebase OAuth token minted
   automatically. Otherwise supply a token yourself (see below).
3. **Network access** to both databases (Takeda network / VPN).
4. **Credentials** (read-only accounts only):
   - Legacy Azure SQL: a SQL-auth reader on `sqldb-lep-data-prod`.
   - Lakebase: a Postgres role on the production `databricks_postgres` DB.

## Setup

```powershell
git clone https://github.com/aatmaprem-takeda/launchpad2-db-migration-validation
cd launchpad2-db-migration-validation
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env
# fill in .env (never committed — see .gitignore)
```

The Lakebase password is a **short-lived OAuth JWT (~1 hour), not your
Databricks PAT**. Supplying the PAT fails with *"Provided authentication token
is not a valid JWT encoding"*. Three ways to supply the JWT, checked in order:

1. Env var `LAUNCHPAD_PROD_DB_OAUTH_TOKEN`
2. First line of `%USERPROFILE%\.launchpad\db-oauth-token`
3. Auto-minted via the Databricks CLI when `LAUNCHPAD_PROD_DATABRICKS_HOST`,
   `LAUNCHPAD_PROD_DATABRICKS_TOKEN` (PAT) and
   `LAUNCHPAD_PROD_LAKEBASE_ENDPOINT` are set in `.env`

Smoke-test both connections before running anything:

```powershell
.venv\Scripts\python launchpad_prod_db.py     # prints CONNECTED ... mode=read-only
```

## Running the validation

```powershell
$env:PYTHONIOENCODING='utf-8'

# Phase 1 — scope & plan pairing (fast, ~1 min)
.venv\Scripts\python phase1_counts.py         # writes out\phase1.json

# Phase 2 — full entity + field comparison (~10–15 min)
.venv\Scripts\python phase2_compare.py        # writes out\phase2.json

# Report — self-contained HTML with every finding explained
.venv\Scripts\python render_db_report.py      # writes out\db-migration-parity-report.html
```

Each phase prints a summary matrix to the terminal; the HTML report carries the
full drill-down (open it in any browser — it has no external dependencies).

## Reading the results

- **Phase 1** must show every in-scope legacy plan paired 1:1 with a LaunchPad
  plan by deterministic ID (87/87 on the reference run), zero missing, zero
  unmatched. Plans outside the three migrated brands (Rusfertide TAK-121,
  Zasocitinib TAK-279, Oveporexton TAK-861) are excluded by design.
- **Phase 2** prints an entity matrix (expected / actual / matched / missing /
  added per entity type) and a field-diff classification table. Any `GAP` row
  is a defect: investigate it before signoff. `STALE_SOURCE` rows are legacy
  edits made after the replica's sync position at migration time — closed by
  re-running the migration closer to cutover (the deterministic-ID upsert
  design makes repeat runs safe), not a code defect.
- One caveat baked into the classifier: legacy `Assignments`,
  `ProjectResources` and `SecondaryLaunchLeads` carry no timestamps, so their
  deltas can only be classified "consistent with post-snapshot churn", never
  timestamp-proven.

## Repository layout

| Path | Role |
|---|---|
| `common.py` | Read-only connectors for both DBs + SELECT-only guard |
| `launchpad_prod_db.py` | Lakebase connector (token handling, read-only enforcement) |
| `phase1_counts.py` | Scope + plan pairing |
| `phase2_compare.py` | Entity presence + field diff + classification |
| `render_db_report.py` | Self-contained HTML report |
| `migration_ref/` | Vendored production migration code — the comparison spec. Do not edit. |
| `PROMPT.md` | Agent prompt to run/extend this validation with an AI coding assistant |
| `out/` | Results (gitignored — contains production data; never commit it) |

## Safety rules

- Never commit `.env`, tokens, or anything in `out/` — the JSON and HTML
  outputs contain production business data (plan names, user emails).
- Never pass `read_only=False` to the Lakebase connector. Writes to either
  production database are out of scope for this repo and are not authorised.
- Treat `migration_ref/` as a frozen reference: it must stay byte-identical to
  the code that ran in production, or the comparison stops being authoritative.
