"""Load ``lep.Notifications`` -> ``notifications``.

``AssignedTo`` -> user (T24); drop notifications with an unresolved user (0.11).
``TaskIDs`` is multi-valued -> take the first (0.11). ``ProjectID`` -> launch_plan.
Idempotent uuid5 + ON CONFLICT (id).

(OptionalMilestones -> launch_plan_events is a separate, low-volume mapping —
deferred; event_type vocab needs confirmation.)
"""
from __future__ import annotations

import argparse
import re

from sqlalchemy import Engine, text

from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.transforms import det_id, user_id_for_email

_FIRST = re.compile(r"[;,|]")


_LABEL = "load_notifications"

_SRC = text('SELECT "ID", "Title", "AssignedTo", "ProjectID", "TaskIDs", "IsRead" '
            'FROM lep."Notifications"')

COLS = ["id", "user_id", "launch_plan_id", "workplan_item_id", "title", "is_read"]


def _first_task(task_ids, items) -> str | None:
    """``TaskIDs`` is multi-valued -> take the first, if it resolves (0.11)."""
    if not task_ids:
        return None
    first = _FIRST.split(str(task_ids))[0].strip()
    cand = det_id("task", first) if first else None
    return cand if cand in items else None


def _read_notifications(conn, maps: ResolutionMaps, plans, items):
    """-> ``(rows, dropped)``; drops notifications whose user cannot resolve (0.11)."""
    rows, dropped = [], 0
    for r in conn.execute(_SRC):
        uid = maps.user_id(r.AssignedTo)
        if not uid:
            dropped += 1
            continue  # 0.11: drop unresolved user
        plan_id = det_id("plan", str(r.ProjectID)) if r.ProjectID else None
        if plan_id and plan_id not in plans:
            plan_id = None
        rows.append({"id": det_id("notif", str(r.ID)), "user_id": uid,
                     "launch_plan_id": plan_id,
                     "workplan_item_id": _first_task(r.TaskIDs, items),
                     "title": (r.Title or "(notification)"), "is_read": bool(r.IsRead)})
    return rows, dropped


def _load_impl(source: Engine, target: Engine, maps: ResolutionMaps, *,
               dry_run: bool = False, _dry_state: dict | None = None) -> int:
    with target.connect() as c:
        if dry_run and _dry_state and "plan_ids" in _dry_state:
            plans = _dry_state["plan_ids"]
        else:
            plans = {str(r.id) for r in c.execute(text("SELECT id FROM lep.launch_plans"))}
        if dry_run and _dry_state and "item_ids" in _dry_state:
            items = _dry_state["item_ids"]
        else:
            items = {str(r.id) for r in c.execute(text("SELECT id FROM lep.workplan_items"))}

    with source.connect() as conn:
        rows, dropped = _read_notifications(conn, maps, plans, items)

    if dry_run:
        print(f"[{_LABEL}] DRY RUN: {len(rows)} notifications ({dropped} dropped, "
              "unresolved user)")
        return len(rows)

    # FK-safe: remap user_id det-uuid5 -> actual DB id; drop unresolvable.
    with target.connect() as c:
        _det_to_actual: dict[str, str] = {}
        for r in c.execute(text("SELECT id, email FROM lep.users")):
            _det_to_actual[user_id_for_email(r.email)] = str(r.id)
    for r in rows:
        r["user_id"] = _det_to_actual.get(r["user_id"])
    before = len(rows)
    rows = [r for r in rows if r["user_id"]]
    fk_dropped = before - len(rows)
    if fk_dropped:
        dropped += fk_dropped
        print(f"[{_LABEL}] dropped {fk_dropped} notifications (user not in target)")

    upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "id")
    up = text(f"INSERT INTO lep.notifications ({', '.join(COLS)}) "
              f"VALUES ({', '.join(':' + c for c in COLS)}) ON CONFLICT (id) DO UPDATE SET {upd}")
    with target.begin() as conn:
        for i in range(0, len(rows), 5000):
            conn.execute(up, rows[i:i + 5000])
    print(f"[{_LABEL}] upserted {len(rows)} notifications ({dropped} dropped)")
    return len(rows)


def load(source: Engine, target: Engine, maps: ResolutionMaps, *, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    return _load_impl(source, target, maps, dry_run=dry_run, _dry_state=_dry_state)


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(); s = source_engine()
    load(s, target_engine(), ResolutionMaps.load(s), dry_run=a.dry_run)
