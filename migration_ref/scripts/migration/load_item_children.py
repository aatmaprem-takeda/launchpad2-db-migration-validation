"""Load workplan-item children: assignments, dependencies, documents, functions,
and country assignments. Runs after ``load_workplan_items``.

  Assignments  -> workplan_item_assignments   (Resource -> user, IsOwner -> is_primary)
  Dependencies -> workplan_item_dependencies   (Type int -> dependency_type)
  TaskDocuments-> workplan_item_documents      (Link, name; links only per 0.5)
  TaskFunctions-> workplan_item_functions      (FunctionId -> functions via Function LT)
  Tasks.AssignedCountries -> workplan_item_country_assignments  (delimited fan-out, T21)

Skips rows whose item/user/country can't resolve. Idempotent uuid5 + ON CONFLICT (id).
"""
from __future__ import annotations

import argparse
import re

from sqlalchemy import Engine, text

from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.transforms import (
    dependency_type, det_id, normalize_function, user_id_for_email,
)
from scripts.migration.writer import report_dry_run, upsert_batches

_SPLIT = re.compile(r"[;,|]")

_LABEL = "load_item_children"


def _has_column(conn, table: str, column: str) -> bool:
    """True if the source table exposes this column (the DMS source drops some)."""
    return bool(conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'lep' AND table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).scalar())


def _target_lookups(target: Engine):
    """``(item ids, function id-or-normalized-name -> id, (bu_id, country name) -> id)``."""
    with target.connect() as c:
        items = {str(r.id) for r in c.execute(text("SELECT id FROM lep.workplan_items"))}
        functions = {}
        for r in c.execute(text("SELECT code, name, id FROM lep.functions")):
            functions[str(r.id)] = str(r.id)  # by id
            nf = normalize_function(r.name)
            if nf:
                functions[nf.lower()] = str(r.id)  # by normalized name
        # Keyed by (business_unit_id, name): `lep.countries` is unique on
        # (business_unit_id, iso_code), NOT on the name, so 11 markets hold one row
        # per managing BU. A name-only dict collapses them to an arbitrary BU's row
        # -- when the country-assignment path below is re-enabled, resolve through
        # the item's plan business_unit_id, never by name alone.
        countries = {(str(r.business_unit_id), r.name.strip().lower()): str(r.id)
                     for r in c.execute(text(
                         "SELECT name, id, business_unit_id FROM lep.countries"))}
    return items, functions, countries


def _read_assignments(conn, items, maps: ResolutionMaps) -> list[dict]:
    """``assignment_type`` is always 'owner' -- the app writes every owner-side row
    that way and distinguishes owner from co-owner by ``is_primary`` (see
    app/features/launchpad/assignment_sql.py), which is exactly what IsOwner gives us.
    It is NOT NULL with no DB default.

    Dedup: 9 users have duplicate Resource GUIDs, causing 73 (item, user)
    collisions.  Keep the ``is_primary=True`` row when both exist."""
    best: dict[tuple[str, str], dict] = {}   # (wi, uid) -> row
    for r in conn.execute(text(
            'SELECT "ID", "Task", "Resource", "IsOwner" FROM lep."Assignments"')):
        wi = det_id("task", str(r.Task))
        uid = maps.user_id(r.Resource)
        if wi in items and uid:
            key = (wi, uid)
            row = {"id": det_id("assignment", str(r.ID)), "workplan_item_id": wi,
                   "user_id": uid, "assignment_type": "owner",
                   "is_primary": bool(r.IsOwner), "is_deleted": False}
            prev = best.get(key)
            if prev is None or (row["is_primary"] and not prev["is_primary"]):
                best[key] = row
    return list(best.values())


def _read_dependencies(conn, items) -> list[dict]:
    """``lag`` is NOT NULL with no DB default; carry the source value when the DMS
    source exposes the column, else fall back to the app default 0."""
    has_lag = _has_column(conn, "Dependencies", "Lag")
    if not has_lag:
        print(f"[{_LABEL}] NOTE source Dependencies has no 'Lag' column -> lag = 0")
    lag_col = ', "Lag"' if has_lag else ""
    rows = []
    for r in conn.execute(text(
            f'SELECT "ID", "FromTask", "ToTask", "Type", "LagUnit"{lag_col} '
            'FROM lep."Dependencies"')):
        f = det_id("task", str(r.FromTask))
        t = det_id("task", str(r.ToTask))
        if f in items and t in items:
            rows.append({"id": det_id("dep", str(r.ID)), "from_workplan_item_id": f,
                         "to_workplan_item_id": t, "dependency_type": dependency_type(r.Type),
                         "lag": int(r.Lag) if has_lag and r.Lag is not None else 0,
                         "lag_unit": (r.LagUnit or "days")})
    return rows


def _read_documents(conn, items) -> list[dict]:
    """TaskDocuments -> workplan_item_documents.

    Maps Name (display label), Description, Link (SharePoint URL), and IsLink.
    The prod source exposes Name, Description, Link, and IsLink; the DMS source
    may drop Name/Link.  Handle both gracefully."""
    has_link = _has_column(conn, "TaskDocuments", "Link")
    has_name = _has_column(conn, "TaskDocuments", "Name")
    cols = '"ID", "TaskId", "Description", "IsLink"'
    if has_name:
        cols += ', "Name"'
    if has_link:
        cols += ', "Link"'
    rows = []
    for r in conn.execute(text(f'SELECT {cols} FROM lep."TaskDocuments"')):
        wi = det_id("task", str(r.TaskId))
        if wi in items:
            name = (getattr(r, "Name", None) or r.Description or "document")
            url = (getattr(r, "Link", None) or "").strip() or None
            rows.append({"id": det_id("doc", str(r.ID)), "workplan_item_id": wi,
                         "name": name, "url": url,
                         "document_type": "link" if r.IsLink else "document",
                         # NOT NULL, no DB default; legacy has no such flag
                         "is_required": False})
    return rows


def _read_item_functions(conn, items, l1_by_plan, src_fns: dict[str, str]) -> list[dict]:
    """FunctionId GUID -> source lep.Functions.Name -> normalize -> plan's L1
    workplan_item whose title matches that function name.

    Since Alembic migration 20260910_0001, ``workplan_item_functions.function_id``
    references ``workplan_items`` (the plan's own L1 row), not ``functions``.
    """
    rows = []
    seen: set[tuple[str, str]] = set()  # (workplan_item_id, function_id) dedup
    for r in conn.execute(text(
            'SELECT "ID", "TaskId", "FunctionId", "ProjectId" '
            'FROM lep."TaskFunctions"')):
        wi = det_id("task", str(r.TaskId))
        plan_id = det_id("plan", str(r.ProjectId))
        fname = normalize_function(src_fns.get(str(r.FunctionId)))
        # Look up the L1 workplan item in *this* plan matching the function name
        fid = l1_by_plan.get((plan_id, (fname or "").lower()))
        if wi in items and fid:
            key = (wi, fid)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"id": det_id("taskfn", str(r.ID)), "workplan_item_id": wi,
                         "function_id": fid, "is_deleted": False})
    return rows


def load(source: Engine, target: Engine, maps: ResolutionMaps, *, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    items, functions, _countries = _target_lookups(target)

    # Build L1-item lookup: (plan_id, normalized_function_name) -> workplan_item_id
    # L1 items have wbs_code with no dots (depth 1)
    l1_by_plan: dict[tuple[str, str], str] = {}
    with target.connect() as c:
        for r in c.execute(text("""
            SELECT id::text, launch_plan_id::text, title
            FROM lep.workplan_items
            WHERE wbs_code IS NOT NULL AND wbs_code NOT LIKE '%%.%%'
              AND is_deleted = false
        """)):
            nf = normalize_function(r.title)
            if nf:
                l1_by_plan[(r[1], nf.lower())] = r[0]
    if dry_run and _dry_state and "item_ids" in _dry_state:
        items = _dry_state["item_ids"]

    with source.connect() as conn:
        # TaskFunctions.FunctionId references lep.Functions directly, not LookupEntries
        src_fns = {str(r.ID): r.Name for r in conn.execute(text(
            'SELECT "ID", "Name" FROM lep."Functions"'))}
        a_rows = _read_assignments(conn, items, maps)
        d_rows = _read_dependencies(conn, items)
        doc_rows = _read_documents(conn, items)
        fn_rows = _read_item_functions(conn, items, l1_by_plan, src_fns)
        # country assignments — DEFERRED: the DMS source `Tasks` has no
        # `AssignedCountries` column (task->country mapping lives elsewhere, TBD).
        # Skip until the real column/table is confirmed so cc_rows stays empty.
        # (Original AssignedCountries fan-out preserved in git history.)
        cc_rows: list[dict] = []

    # FK-safe: remap assignment user_id det-uuid5 -> actual DB id; drop unresolvable.
    if not dry_run:
        with target.connect() as c:
            _det_to_actual: dict[str, str] = {}
            for r in c.execute(text("SELECT id, email FROM lep.users")):
                _det_to_actual[user_id_for_email(r.email)] = str(r.id)
        for r in a_rows:
            r["user_id"] = _det_to_actual.get(r["user_id"])
        before = len(a_rows)
        a_rows = [r for r in a_rows if r["user_id"]]
        dropped = before - len(a_rows)
        if dropped:
            print(f"[{_LABEL}] dropped {dropped} assignments (user not in target)")

    batches = [
        ("workplan_item_assignments", a_rows,
         ["id", "workplan_item_id", "user_id", "assignment_type", "is_primary", "is_deleted"]),
        ("workplan_item_dependencies", d_rows,
         ["id", "from_workplan_item_id", "to_workplan_item_id", "dependency_type",
          "lag", "lag_unit"]),
        ("workplan_item_documents", doc_rows,
         ["id", "workplan_item_id", "name", "url", "document_type", "is_required"]),
        ("workplan_item_functions", fn_rows,
         ["id", "workplan_item_id", "function_id", "is_deleted"]),
        ("workplan_item_country_assignments", cc_rows,
         ["id", "workplan_item_id", "country_id", "assignment_status", "is_required"]),
    ]
    if dry_run:
        return report_dry_run(batches, label=_LABEL)

    total = upsert_batches(target, batches, label=_LABEL)
    print(f"[{_LABEL}] upserted {total} child rows")
    return total


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(); s = source_engine()
    load(s, target_engine(), ResolutionMaps.load(s), dry_run=a.dry_run)
