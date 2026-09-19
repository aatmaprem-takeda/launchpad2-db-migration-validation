# LEP transactional migration ETL

Legacy **SQL Server `lep.*`** (Azure `sqldb-lep-data-dev`, the system of record)
→ **PostgreSQL `lep`** (the FastAPI target). Loads the transactional tables the
seed scripts don't cover. Spec: [`../../docs/migration/lep-data-migration-guide.md`](../../docs/migration/lep-data-migration-guide.md).

## Prerequisites (run first, in order)
1. `alembic upgrade head` — target schema
2. `psql -f scripts/seed_bu_geography.sql` — geography (4 BU / 8 area / 15 cluster / 193 country)
3. `python -m scripts.seed_from_dump` — product + users seed
4. Legacy source reachable (see connection env below)

## Configure the source (legacy SQL Server)
```bash
export LEP_SRC_SERVER="sql-lep-data-dev.database.windows.net"
export LEP_SRC_DB="sqldb-lep-data-dev"
export LEP_SRC_USER="LE-DBA-DEV"
export LEP_SRC_PASSWORD="********"      # or set LEP_SRC_AUTH=aad for Azure AD
# target PostgreSQL uses the app's existing settings (app.core.config)
```
Requires an ODBC driver (`ODBC Driver 18 for SQL Server`) and `pyodbc` (already installed).

## Run
```bash
python -m scripts.migration.transforms          # pure self-check, no DB
python -m scripts.migration.run_all --dry-run   # counts only, no writes
python -m scripts.migration.run_all             # real load
```

## Modules
| Module | Reads (`lep.*`) | Writes | Status |
| --- | --- | --- | --- |
| `db` | — | — | engines (source/target) |
| `transforms` | — | — | ✅ verified crosswalks (§6.3), self-tested |
| `resolution_maps` | `LookupEntries`, `Lookups`, `Resources` | *(in-memory)* | ✅ |
| `load_users` | `Resources` | `users` | ✅ unblocked (4,078 users) |
| `load_plans` | `Projects` | `launch_plans` | ⛔ blocked: GEO-3, 0.18 |
| `load_workplan_items` | `Tasks` (200K) | `workplan_items` | ⛔ needs plans; batch load |
| `load_item_children` | `Assignments`, `Dependencies`, `TaskDocuments`, `TaskFunctions` | item child tables | ⛔ needs items |
| `load_memberships` | `ProjectResources`, `SecondaryLaunchLeads`, … | `plan_memberships` | ⛔ needs plans (role=function, 0.14) |
| `load_trackers` / `load_notifications` | `Tracker*`, `Notifications` | `trackers`, `notifications` | ⛔ needs plans |

## Open blockers (before plan loader)
- **GEO-3** — 57 Global plans → OBU/SPD (business); see `docs/migration/GEO-3-global-plan-BU-decision.xlsx`.
- **0.18** — 140 dual-parent plans → single `parent_plan_id`; recommend `global_plan_id` + `mce_plan_id`.

Everything upstream (users, resolution maps, transforms) is unblocked and runnable now.
