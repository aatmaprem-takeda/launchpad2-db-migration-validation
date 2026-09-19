"""LEP transactional data-migration ETL (legacy SQL Server ``lep.*`` -> PostgreSQL ``lep``).

Reads the legacy **system of record** (`lep.*` in Azure SQL `sqldb-lep-data-dev`)
and loads the transactional tables the seed scripts do not cover: users, launch
plans, workplan items, assignments, dependencies, memberships, trackers,
notifications.

Read the master spec first: ``docs/migration/lep-data-migration-guide.md``
(counts in §3.1, plan classification §3.5, verified crosswalks §6.3, load order
§6.1, open decisions §7). All figures here were verified against the live DB on
2026-07-30.

Modules
    db              -- source (SQL Server) + target (PostgreSQL) engines
    transforms      -- deterministic IDs + the verified value crosswalks (§6.3)
    resolution_maps -- {LookupEntries.id -> Name} / {Resources.id -> email} maps
    load_users      -- lep.Resources -> lep.users            (no blocker)
    load_plans      -- lep.Projects  -> lep.launch_plans     (needs GEO-3 / 0.18)
    run_all         -- orchestrator, FK-safe order, one txn per stage
"""
