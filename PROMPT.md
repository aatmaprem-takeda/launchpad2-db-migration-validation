# Agent prompt — LEP to LaunchPad 2.0 DB migration validation

Copy the prompt below into your AI coding assistant (GitHub Copilot CLI /
agent mode) from the root of this repository. It reproduces the full
database-to-database validation and drives the investigation of any
differences it finds. Everything it does is read-only.

Fill in the two `<...>` placeholders before pasting if your credentials are
not already configured (see README → Setup).

---

## The prompt

```
You are validating that the LaunchPad 2.0 production database (Lakebase
Postgres, database "databricks_postgres") faithfully contains everything the
migration was supposed to copy from the legacy LEP production database
(Azure SQL, sqldb-lep-data-prod). Work strictly read-only on both systems —
every query must go through common.py's q()/qpg() helpers, which refuse
non-SELECT statements. Never weaken that guard.

Context you must use, not re-derive:
- migration_ref/ contains the exact migration code that ran in production
  (origin/main @ 26166fc). The production re-migration run id is
  run-20260919T120011Z, executed 2026-09-19 12:00–12:03 UTC. Its
  transforms.py, scope.py, load_*.py and
  deterministic uuid5 IDs are the specification for what "correctly migrated"
  means. Import them; do not re-implement them.
- The migration reads a continuously-syncing DMS replica of legacy (the
  Lakebase database named "launchpad" on the same instance), not live legacy
  directly. Any legacy edit made after the replica's sync position at
  migration time is expected to be absent from LaunchPad. phase2_compare.py
  encodes this as SNAPSHOT_CUTOFF — recompute it for a new run as
  max(updated_at) among target workplan_items with updated_at before the
  run's start.
- lep.migration_audit in the target database records the run's stages and row
  counts — cross-check against it.

Step 1 — connectivity. Run `python launchpad_prod_db.py` and a 1-row SELECT
through common.legacy_engine(). Stop and report if either side fails; do not
improvise alternative connection paths.

Step 2 — scope and pairing. Run phase1_counts.py. Confirm every in-scope
legacy plan (brands TAK-121, TAK-279, TAK-861 per migration_ref scope.py)
pairs 1:1 with a LaunchPad plan via the migration's det_id. Show me the
counts per brand and any missing/unmatched plan before continuing.

Step 3 — full comparison. Run phase2_compare.py. Show the entity matrix
(expected/actual/matched/missing/added per entity) and the field-diff
classification table in the terminal.

Step 4 — investigate, in this order, and show your evidence for each claim:
- Any GAP row is a potential migration defect. For each, pull the exact row
  from both databases, check legacy audit tables (lep.ProjectsAudit,
  lep.TasksAudit — OldRowData/NewRowData JSON, Created) to establish what the
  value was at snapshot time, and only then classify it as defect or
  post-snapshot edit.
- Sanity-check a sample of STALE_SOURCE rows the same way: the legacy
  LastUpdatedDate must be after 2026-08-31 01:25:46 UTC. Beware: legacy has
  mass batch updates that touch timestamps without changing data, and a few
  channels that change data without touching timestamps — audit tables are
  the tiebreaker.
- Do not trust legacy Assignments / ProjectResources / SecondaryLaunchLeads
  timestamps — those tables have none. Say so explicitly when classifying
  their deltas.

Step 5 — report. Run render_db_report.py and give me the path to
out/db-migration-parity-report.html. Then print a final verdict: the GAP
count (must be 0 for signoff), what every non-zero bucket is explained by,
and any open business decision (e.g. accepting the stale snapshot vs
re-running the migration from a fresh replica).

Rules: never run INSERT/UPDATE/DELETE/DDL on either database; never commit
.env or anything in out/; if a number surprises you, show the query and the
raw result before interpreting it.
```

---

## What a clean run looks like (2026-09-19 re-migration, run-20260919T120011Z)

- Phase 1: 87/87 plans paired, 0 missing, 0 unmatched; 717 out-of-scope
  plans excluded by brand scoping.
- Phase 2: zero missing rows on every entity; 0 GAP task field diffs, 0 plan
  field diffs. Remaining buckets explained: whitespace trimmed by a
  post-migration cleanup, designed WBS renumbering, injection milestone
  flags, and post-migration app activity (memberships, recomputed statuses).
- Membership note: the loader deliberately drops redundant roles (user is
  the plan's launch lead; team_member who is already a secondary launch lead
  on the same plan). The comparator replicates those drops.

If your run shows GAP > 0, that is new information: either the databases
changed, or a classification rule needs review. Investigate before assuming
either.
