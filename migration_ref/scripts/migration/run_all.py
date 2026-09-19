"""Orchestrate the transactional migration in FK-safe order (guide §6.1).

By default this migrates ONLY the in-scope brands (scripts/migration/scope.py) —
that scoped set IS the migration. Pass --full (or LEP_MIGRATION_SCOPE=full) to
migrate every non-template plan instead.

    # dry run (counts only, no writes, no audit) — scoped to the migration brands:
    python -m scripts.migration.run_all --dry-run

    # real load (writes an audit trail to lep.migration_audit + a JSON report):
    python -m scripts.migration.run_all

    # migrate the ENTIRE legacy DB instead of just the brands:
    python -m scripts.migration.run_all --full

Reference/master + static config + geography are seeded elsewhere
(seed_bu_geography.sql, seed_from_dump.py, Alembic) — run those FIRST.

Every real run is recorded per-stage to lep.migration_audit; a full run also
reconciles source vs target row counts at the end (skipped for a scoped run,
since the source counts are full-DB) — the migration's durable audit trail.
"""
from __future__ import annotations

import argparse
import os

from scripts.migration import (load_audit, load_comments, load_functions,
                               load_injections, load_item_children,
                               load_memberships, load_notifications,
                               load_plans, load_templates, load_trackers,
                               load_users, load_workplan_items)
from scripts.migration.audit import MigrationAudit, timed
from scripts.migration.db import source_engine, target_engine
from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.scope import is_full_migration, scope_label

# (stage label, target table, loader callable factory) — order is FK-safe, do not reorder.
def _plan(src, tgt, maps, dry):
    _state = {}  # dry-run ID cascading: loaders publish/consume plan_ids, item_ids
    return [
        ("users", "users", lambda: load_users.load(src, tgt, maps=maps, dry_run=dry, _dry_state=_state)),
        ("functions", "functions", lambda: load_functions.load(src, tgt, dry_run=dry)),
        ("plans", "launch_plans", lambda: load_plans.load(src, tgt, maps, dry_run=dry, _dry_state=_state)),
        ("workplan_items", "workplan_items", lambda: load_workplan_items.load(src, tgt, maps, dry_run=dry, _dry_state=_state)),
        ("item_children", "workplan_item_*", lambda: load_item_children.load(src, tgt, maps, dry_run=dry, _dry_state=_state)),
        ("comments", "workplan_item_comments", lambda: load_comments.load(src, tgt, maps, dry_run=dry, _dry_state=_state)),
        ("memberships", "plan_memberships", lambda: load_memberships.load(src, tgt, maps, dry_run=dry, _dry_state=_state)),
        ("trackers", "trackers", lambda: load_trackers.load(src, tgt, dry_run=dry, _dry_state=_state)),
        ("notifications", "notifications", lambda: load_notifications.load(src, tgt, maps, dry_run=dry, _dry_state=_state)),
        ("injections", "workplan_item_injections", lambda: load_injections.load(src, tgt, dry_run=dry, _dry_state=_state)),
        ("templates", "launch_templates", lambda: load_templates.load(src, tgt, maps, dry_run=dry, _dry_state=_state)),
        ("audit", "audit_logs", lambda: load_audit.load(src, tgt, maps, dry_run=dry, _dry_state=_state)),
    ]


def _parse_args():
    ap = argparse.ArgumentParser(description="LEP transactional migration")
    ap.add_argument("--dry-run", action="store_true", help="counts only, no writes, no audit")
    ap.add_argument("--full", action="store_true",
                    help="migrate EVERY non-template plan (default: only the scope.py brands)")
    return ap.parse_args()


def _run_stages(src, tgt, maps, dry_run, audit) -> bool:
    """Run every stage in FK-safe order, recording each to the audit trail.
    Returns True if a stage failed (downstream stages are then skipped)."""
    for name, tbl, fn in _plan(src, tgt, maps, dry_run):
        print(f"== {name} ==")
        result, ms, err = timed(fn)
        if audit:
            audit.stage(name, tbl, None if err else result,
                        "error" if err else "ok",
                        message=str(err)[:500] if err else None, duration_ms=ms)
        if err:
            print(f"[run_all] STAGE FAILED: {name}: {err}")
            return True   # FK-safe: don't run downstream stages after a failure
    return False


def _finish_audit(audit, src, failed: bool) -> None:
    """Reconcile (full runs only), close the run, write the JSON report."""
    if not failed and is_full_migration():
        print("== reconcile (source vs target) ==")
        audit.reconcile(src)
    elif not failed:
        # RECON compares FULL-DB source counts vs target; for a scoped run those
        # deltas are expected, so skip it and verify the scoped counts directly.
        print(f"== reconcile SKIPPED — scope is {scope_label()}; "
              "source counts are full-DB, verify the scoped counts directly ==")
    audit.run("error" if failed else "ok")
    path = audit.write_report()
    print(f"[audit] {audit.seq} rows -> lep.migration_audit + {path}")


def main() -> None:
    args = _parse_args()

    if args.full:
        os.environ["LEP_MIGRATION_SCOPE"] = "full"  # before scope is read
    print(f"== scope: {scope_label()} ==")

    src, tgt = source_engine(), target_engine()
    audit = None if args.dry_run else MigrationAudit(tgt)
    if audit:
        print(f"== audit run_id: {audit.run_id} ==")

    print("== resolution maps ==")
    maps = ResolutionMaps.load(src)
    print(f"[resolution_maps] {maps.summary()}")

    failed = _run_stages(src, tgt, maps, args.dry_run, audit)

    if audit:
        _finish_audit(audit, src, failed)

    print("Dry run complete." if args.dry_run else ("FAILED." if failed else "Done."))


if __name__ == "__main__":
    main()
