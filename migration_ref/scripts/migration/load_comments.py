"""Load ``lep.Tasks.Note`` -> ``lep.workplan_item_comments``.

Each legacy task with a non-empty ``Note`` column gets a single comment row in
the target. Runs after ``load_workplan_items`` (FK ``workplan_item_id``).

Author resolution: ``Tasks.LastUpdatedBy`` is a display name (not a GUID), so
we build a name->email reverse lookup from ``lep.Resources`` and resolve to the
target user.  Unresolvable authors ("System", None, etc.) leave ``author_id``
NULL — these are system-generated notes from the legacy DMS migration.

Idempotent: ``id = uuid5('comment', task_guid)``, ``ON CONFLICT (id)``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Engine, text

from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.transforms import det_id, user_id_for_email
from scripts.migration.writer import Batch, report_dry_run, upsert_batches

_LABEL = "load_comments"


def load(source: Engine, target: Engine, maps: ResolutionMaps, *,
         dry_run: bool = False, _dry_state: dict | None = None) -> int:
    """Migrate non-empty Tasks.Note to workplan_item_comments."""

    # Get the set of migrated workplan_item IDs already in the target
    with target.connect() as c:
        item_ids = {str(r.id) for r in c.execute(
            text("SELECT id FROM lep.workplan_items")
        )}
    print(f"[{_LABEL}] target workplan_items: {len(item_ids)}")

    # Also get target user IDs for FK validation
    with target.connect() as c:
        user_ids = {str(r.id) for r in c.execute(
            text("SELECT id FROM lep.users")
        )}

    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    skipped_no_item = 0

    with source.connect() as c:
        src_rows = c.execute(text("""
            SELECT "ID"::text      AS task_guid,
                   "Note",
                   "LastUpdatedBy",
                   "LastUpdatedDate",
                   "CreatedDate"
            FROM lep."Tasks"
            WHERE "Note" IS NOT NULL AND "Note" != ''
        """)).fetchall()

    for r in src_rows:
        # Map task GUID -> workplan_item_id via det_id
        wi_id = det_id("task", r.task_guid)
        if wi_id not in item_ids:
            skipped_no_item += 1
            continue

        # Deterministic comment ID (one comment per task)
        comment_id = det_id("comment", r.task_guid)

        # Resolve author: LastUpdatedBy is an email address (or "System" / NULL).
        # Resolve directly via user_id_for_email, validate against target users.
        author_id = None
        last_by = (r.LastUpdatedBy or "").strip().lower()
        if last_by and "@" in last_by:
            uid = user_id_for_email(last_by)
            if uid in user_ids:
                author_id = uid

        ts = r.LastUpdatedDate or r.CreatedDate or now

        rows.append({
            "id":               comment_id,
            "workplan_item_id": wi_id,
            "author_id":        author_id,
            "body":             r.Note,
            "comment_type":     "note",
            "is_deleted":       False,
            "description":      None,
            "created_by":       author_id,
            "updated_by":       author_id,
            "created_at":       ts,
            "updated_at":       ts,
        })

    cols = ["id", "workplan_item_id", "author_id", "body", "comment_type",
            "is_deleted", "description", "created_by", "updated_by",
            "created_at", "updated_at"]
    batches: list[Batch] = [("workplan_item_comments", rows, cols)]

    print(f"[{_LABEL}] source Notes with matching item: {len(rows)}")
    print(f"[{_LABEL}] skipped (item not in target): {skipped_no_item}")
    with_author = sum(1 for r in rows if r["author_id"] is not None)
    print(f"[{_LABEL}] resolved author: {with_author}/{len(rows)}")

    if dry_run:
        return report_dry_run(batches, label=_LABEL)
    return upsert_batches(target, batches, label=_LABEL)


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    from scripts.migration.resolution_maps import ResolutionMaps

    src, tgt = source_engine(), target_engine()
    maps = ResolutionMaps.load(src)
    n = load(src, tgt, maps)
    print(f"Done: {n} comments loaded")
