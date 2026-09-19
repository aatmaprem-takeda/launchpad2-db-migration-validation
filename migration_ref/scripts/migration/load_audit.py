"""Migrate ``ProjectsAudit`` + ``TasksAudit`` -> ``lep.audit_logs``.

Carries the legacy change history so users can see who edited plans and tasks,
when, and what changed.  Scoped to in-scope plans only.  Idempotent:
``id = uuid5('plan_audit'|'task_audit', AuditID)``; ``ON CONFLICT (id)``.

Run standalone:  python -m scripts.migration.load_audit --dry-run
"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import Engine, text

from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.scope import in_scope
from scripts.migration.transforms import det_id, user_id_for_email

_BATCH = 2000

_UPSERT_COLS = [
    "id", "occurred_at", "action", "entity_type", "entity_id",
    "actor_id", "actor_email", "outcome", "description", "changes",
]


def _diff(old_json: str | None, new_json: str | None) -> dict | None:
    """Compute a field-level diff between two JSON row snapshots."""
    try:
        old = json.loads(old_json) if old_json else {}
        new = json.loads(new_json) if new_json else {}
    except (json.JSONDecodeError, TypeError):
        return None
    changes = {}
    for k in sorted(set(old) | set(new)):
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            changes[k] = {"old": ov, "new": nv}
    return changes or None


def _actor_email_from_json(new_json: str | None) -> str | None:
    """Extract the acting user's email from the new-row JSON snapshot."""
    try:
        data = json.loads(new_json) if new_json else {}
    except (json.JSONDecodeError, TypeError):
        return None
    return (data.get("LastUpdatedBy") or data.get("CreatedBy") or "").strip().lower() or None


def _in_scope_plan_ids(source: Engine, maps: ResolutionMaps) -> set[str]:
    """Legacy Project GUIDs that pass the migration scope filter."""
    ids: set[str] = set()
    with source.connect() as conn:
        for p in conn.execute(text(
                'SELECT "ID", "FranchiseBrandIndication", "IsTemplate" '
                'FROM lep."Projects"')).mappings():
            if p.get("IsTemplate"):
                continue
            fbi = maps.entry_name(p.get("FranchiseBrandIndication"))
            if in_scope(fbi):
                ids.add(str(p.get("ID")).upper())
    return ids


def _in_scope_task_ids(source: Engine, plan_ids: set[str]) -> set[str]:
    """Legacy Task GUIDs belonging to in-scope plans."""
    ids: set[str] = set()
    with source.connect() as conn:
        for t in conn.execute(text('SELECT "ID", "ProjectID" FROM lep."Tasks"')).mappings():
            if str(t.get("ProjectID")).upper() in plan_ids:
                ids.add(str(t.get("ID")).upper())
    return ids


def _build_rows(source: Engine, table: str, entity_type: str, id_prefix: str,
                entity_id_ns: str, valid_ids: set[str],
                known_users: dict[str, str]) -> list[dict]:
    """Read one source audit table and build target audit_logs rows."""
    rows: list[dict] = []
    with source.connect() as conn:
        for r in conn.execute(text(f'SELECT * FROM lep."{table}"')).mappings():
            legacy_id = str(r.get("ID") or "").upper()
            if legacy_id not in valid_ids:
                continue

            email = _actor_email_from_json(r.get("NewRowData") or r.get("OldRowData"))
            uid = user_id_for_email(email) if email else None
            # FK-safe: remap det-uuid5 -> actual DB id
            if uid:
                uid = known_users.get(uid)

            audit_type = (r.get("AuditType") or "update").strip().lower()
            changes = _diff(r.get("OldRowData"), r.get("NewRowData"))

            rows.append({
                "id": det_id(id_prefix, str(r.get("AuditID"))),
                "occurred_at": r.get("Created"),
                "action": f"{audit_type} {entity_type}",
                "entity_type": entity_type,
                "entity_id": det_id(entity_id_ns, legacy_id),
                "actor_id": uid,
                "actor_email": email,
                "outcome": "success",
                "description": f"Legacy {audit_type}: {entity_type}",
                "changes": json.dumps(changes) if changes else None,
            })
    return rows


def load(source: Engine, target: Engine, maps: ResolutionMaps, *,
         dry_run: bool = False, _dry_state: dict | None = None) -> int:
    # Scope
    plan_ids = _in_scope_plan_ids(source, maps)
    task_ids = _in_scope_task_ids(source, plan_ids)

    # Known users for FK-safe actor_id: det-uuid5 -> actual DB id
    known_users: dict[str, str] = {}
    if not dry_run:
        with target.connect() as c:
            for r in c.execute(text("SELECT id, email FROM lep.users")):
                known_users[user_id_for_email(r.email)] = str(r.id)

    plan_rows = _build_rows(source, "ProjectsAudit", "launch_plan", "plan_audit",
                            "plan", plan_ids, known_users)
    task_rows = _build_rows(source, "TasksAudit", "workplan_item", "task_audit",
                            "task", task_ids, known_users)
    all_rows = plan_rows + task_rows

    if dry_run:
        print(f"[load_audit] DRY RUN: {len(plan_rows)} plan audit + {len(task_rows)} task audit "
              f"= {len(all_rows)} audit_logs — no write")
        return len(all_rows)

    # audit_logs has PostgreSQL RULES that block ON CONFLICT.
    # Strategy: delete any existing migration rows, then plain INSERT.
    collist = ", ".join(_UPSERT_COLS)
    params = ", ".join(f":{c}" for c in _UPSERT_COLS)
    insert_sql = text(f"INSERT INTO lep.audit_logs ({collist}) VALUES ({params})")

    # audit_logs has RULES: no_delete + no_update (append-only).
    # Can't upsert or clean up — skip rows that already exist.
    with target.connect() as c:
        existing_ids = {str(r[0]) for r in c.execute(
            text("SELECT id FROM lep.audit_logs WHERE description LIKE 'Legacy %'"))}
    if existing_ids:
        before = len(all_rows)
        all_rows = [r for r in all_rows if r["id"] not in existing_ids]
        print(f"[load_audit] {before - len(all_rows)} rows already exist, inserting {len(all_rows)} new")

    written = 0
    with target.begin() as conn:
        # Insert fresh
        for i in range(0, len(all_rows), _BATCH):
            chunk = [{c: r.get(c) for c in _UPSERT_COLS} for r in all_rows[i:i + _BATCH]]
            conn.execute(insert_sql, chunk)
            written += len(chunk)
    print(f"[load_audit] inserted {written} audit_logs "
          f"({len(plan_rows)} plan + {len(task_rows)} task)")
    return written


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    src = source_engine()
    load(src, target_engine(), ResolutionMaps.load(src), dry_run=args.dry_run)
