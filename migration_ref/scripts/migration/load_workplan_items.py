"""Load ``lep.Tasks`` -> ``lep.workplan_items`` (the WBS tree, ~200K rows).

Runs after ``load_plans`` (FK ``launch_plan_id`` -> ``launch_plans``). Verified
§6.3 crosswalks: ``LepStatus`` -> ``status`` (workflow), ``Status`` (RAG) +
``IsAtRisk`` -> ``is_at_risk``. Parent-before-child is guaranteed by sorting on
``OutlineLevel`` (depth) before insert, so a ``parent_id`` self-FK always resolves.
Batched upserts (executemany) for the row volume. Idempotent: ``id =
uuid5('task', legacy ID)``, ``ON CONFLICT (id)``.

Tasks whose plan was not loaded (template plans, filtered in load_plans) are
skipped so they can't orphan.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from sqlalchemy import Engine, text

from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.transforms import det_id, task_is_at_risk, task_status, user_id_for_email

import re as _re

_SRC = text('SELECT * FROM lep."Tasks"')

# Legacy LookupEntries stores recommended-date offsets as e.g. "-12", "-10m",
# "-60d".  The new app expects the "L" prefix: "L-12m", "L-10m", "L-60d" (regex
# ^L[+-]\d+[dmy]$).  Values without a unit suffix (e.g. "-12") default to months
# per legacy convention.  Non-numeric entries (e.g. "TradeStockAvailableDate")
# are dropped as unmappable.
_OFFSET_RE = _re.compile(r'^([+-]?\d+)([dmy])?$', _re.IGNORECASE)


def _recommended_key(raw: str | None) -> str | None:
    """Transform legacy offset name to app-format key (L±Nd/m/y)."""
    if not raw:
        return None
    m = _OFFSET_RE.match(raw.strip())
    if not m:
        return None  # e.g. 'TradeStockAvailableDate' — not an offset
    amount = m.group(1)  # includes sign if present, e.g. "-12" or "6"
    unit = (m.group(2) or 'm').lower()  # default to months
    # Ensure sign prefix: bare "6" -> "+6"
    if not amount.startswith(('+', '-')):
        amount = '+' + amount
    return f'L{amount}{unit}'


def _date_offset_key(actual_date, cld) -> str | None:
    """Fallback: derive L±Nd from (actual_date − commercial_launch_date)."""
    if actual_date is None or cld is None:
        return None
    delta = (actual_date - cld).days
    sign = '+' if delta >= 0 else '-'
    return f'L{sign}{abs(delta)}d'
_BATCH = 5000

# Fixed column set so executemany is uniform (omit NOT-NULL-with-default cols we
# don't map -> the DB default applies).
COLS = [
    "id", "launch_plan_id", "parent_id", "wbs_code", "title", "status",
    "start_date", "due_date", "completed_date", "is_milestone", "is_at_risk",
    "is_private", "is_hidden", "is_deleted", "is_mandatory", "importance",
    "recommended_start_key", "recommended_end_key", "sort_order",
    "level_definition_id",
    "created_at", "updated_at", "created_by", "updated_by",
]

# WBS is a uniform 4-level hierarchy (L1 Function, L2 Activity, L3 Task,
# L4 Subtask) — same for every BU/plan type (Rovo 2026-08-03). Legacy OutlineLevel
# maps 1:1 to level_number; anything >=5 caps at L4 (LaunchPad blocks/hides L5).
MAX_LEVEL = 4


def _level_id(outline, level_by_num):
    """OutlineLevel -> level_definition_id. 1..4 -> L1..L4; >=5 caps at L4;
    0/None (root/summary — above L1) -> NULL (no L-level in the 4-level scheme)."""
    if not isinstance(outline, int) or outline < 1:
        return None
    return level_by_num.get(min(outline, MAX_LEVEL))


def _normalize_wbs(value: object) -> str | None:
    """Strip stray leading/trailing dots from a legacy WBS value.

    Mirrors app/features/templates/xlsx_parser.py's _normalize_wbs: legacy
    source data can carry an artifact like "8.1.2.2.", which the app never
    produces itself but which breaks any string_to_array(...)::int[] cast
    downstream (e.g. DATE_AUTOMATION_SQL's ORDER BY).
    """
    if value is None:
        return None
    cleaned = " ".join(str(value).replace("\xa0", " ").split()).strip(".")
    return cleaned or None


def _wbs_key(wbs: str | None):
    """Natural sort key for a dotted WBS like '1.2.10'."""
    if not wbs:
        return ()
    out = []
    for part in str(wbs).split("."):
        out.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(out)


def load(source: Engine, target: Engine, maps: ResolutionMaps, *, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    # valid plan ids (load_plans already ran) — skip tasks of unloaded/template plans
    with target.connect() as c:
        if dry_run and _dry_state and "plan_ids" in _dry_state:
            valid_plans = _dry_state["plan_ids"]
        else:
            valid_plans = {str(r.id) for r in c.execute(text("SELECT id FROM lep.launch_plans"))}
        # plan_type -> {level_number: level_definition_id}
        # Each plan type uses its own framework (GLOBAL/MCE/LOC); none is marked
        # is_default, so we map by framework name keyword instead.
        _fw_levels: dict[str, dict[int, str]] = {}
        for r in c.execute(text(
                "SELECT lf.name, wld.level_number, wld.id "
                "FROM lep.launch_frameworks lf "
                "JOIN lep.wbs_level_definitions wld ON wld.framework_id = lf.id")):
            fname = (r.name or "").upper()
            pt = "global" if "GLOBAL" in fname else ("mce" if "MCE" in fname else "local")
            _fw_levels.setdefault(pt, {})[r.level_number] = str(r.id)
        # plan_id -> plan_type (needed to pick the right framework per item)
        if dry_run and _dry_state and "plan_types" in _dry_state:
            _plan_types = _dry_state["plan_types"]
        else:
            _plan_types = {str(r.id): r.plan_type for r in c.execute(
                text("SELECT id, plan_type FROM lep.launch_plans"))}
        # plan_id -> CLD (for recommended-date fallback)
        _plan_clds: dict[str, object] = {}
        for r in c.execute(text(
                "SELECT id, commercial_launch_date FROM lep.launch_plans "
                "WHERE commercial_launch_date IS NOT NULL")):
            _plan_clds[str(r.id)] = r.commercial_launch_date

    rows = []
    with source.connect() as conn:
        for t in conn.execute(_SRC).mappings():
            plan_id = det_id("plan", str(t.get("ProjectID")))
            if plan_id not in valid_plans:
                continue
            rag = maps.entry_name(t.get("Status"))
            rows.append({
                "id": det_id("task", str(t.get("ID"))),
                "launch_plan_id": plan_id,
                "parent_id": det_id("task", str(t.get("ParentID"))) if t.get("ParentID") else None,
                "wbs_code": _normalize_wbs(t.get("WBS")),
                "title": t.get("Name") or "(untitled)",
                "status": task_status(maps.entry_name(t.get("LepStatus"))),
                "start_date": t.get("StartDate"),
                "due_date": t.get("EndDate"),
                "completed_date": t.get("CompletedDate"),
                "is_milestone": bool(t.get("IsMilestone")),
                "is_at_risk": bool(t.get("IsAtRisk")) or task_is_at_risk(rag),
                "is_private": bool(t.get("IsPrivate")),
                "is_hidden": bool(t.get("IsHidden")),
                "is_deleted": False,   # lep.Tasks has no delete flag
                "is_mandatory": (t.get("OutlineLevel") or 0) in (1, 2),  # L1/L2 structural protection
                "importance": maps.entry_name(t.get("Importance")),
                "recommended_start_key": (
                    _recommended_key(maps.entry_name(t.get("RecommendedStart")))
                    or _date_offset_key(t.get("StartDate"), _plan_clds.get(plan_id))
                ),
                "recommended_end_key": (
                    _recommended_key(maps.entry_name(t.get("RecommendedEnd")))
                    or _date_offset_key(t.get("EndDate"), _plan_clds.get(plan_id))
                ),
                "level_definition_id": _level_id(
                    t.get("OutlineLevel"),
                    _fw_levels.get(_plan_types.get(plan_id, "local"), {}),
                ),
                "created_at": t.get("CreatedDate") or t.get("LastUpdatedDate"),
                "updated_at": t.get("LastUpdatedDate") or t.get("CreatedDate"),
                "created_by": user_id_for_email((t.get("CreatedBy") or "").strip().lower()) or None,
                "updated_by": user_id_for_email((t.get("LastUpdatedBy") or "").strip().lower()) or None,
                "_outline": t.get("OutlineLevel") or 0,   # sort helper (dropped before insert)
                "_wbs": _wbs_key(t.get("WBS")),
            })

    # parent-before-child: shallower OutlineLevel first, WBS order within
    rows.sort(key=lambda r: (r["launch_plan_id"], r["_outline"], r["_wbs"]))
    # per-plan sort_order
    seq: dict[str, int] = {}
    for r in rows:
        seq[r["launch_plan_id"]] = seq.get(r["launch_plan_id"], 0) + 1
        r["sort_order"] = seq[r["launch_plan_id"]]
        r.pop("_outline", None); r.pop("_wbs", None)

    # Dedup: source may have duplicate WBS codes per plan; the target enforces
    # UNIQUE(launch_plan_id, wbs_code) WHERE is_deleted=false AND wbs_code IS NOT NULL.
    # Keep the first (by sort order) and NULL the duplicate's wbs_code.
    _seen_wbs: set[tuple[str, str]] = set()
    deduped = 0
    _deduped_plans: set[str] = set()
    for r in rows:
        wbs = r.get("wbs_code")
        if not wbs:
            continue
        key = (r["launch_plan_id"], wbs)
        if key in _seen_wbs:
            r["wbs_code"] = None
            deduped += 1
            _deduped_plans.add(r["launch_plan_id"])
        else:
            _seen_wbs.add(key)
    if deduped:
        print(f"[load_workplan_items] deduped {deduped} duplicate WBS codes (set to NULL)")

    # FK-safe: created_by/updated_by must reference a loaded user.
    # Remap deterministic uuid5 -> actual DB id (handles app-provisioned users).
    if not dry_run:
        with target.connect() as c:
            _det_to_actual: dict[str, str] = {}
            for r in c.execute(text("SELECT id, email FROM lep.users")):
                _det_to_actual[user_id_for_email(r.email)] = str(r.id)
        for r in rows:
            if r.get("created_by"):
                r["created_by"] = _det_to_actual.get(r["created_by"])
            if r.get("updated_by"):
                r["updated_by"] = _det_to_actual.get(r["updated_by"])

    if dry_run:
        if _dry_state is not None:
            _dry_state["item_ids"] = {r["id"] for r in rows}
        print(f"[load_workplan_items] DRY RUN: {len(rows)} items across {len(seq)} plans — no write")
        return len(rows)

    params = ", ".join(f":{c}" for c in COLS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS if c != "id")
    upsert = text(
        f"INSERT INTO lep.workplan_items ({', '.join(COLS)}) VALUES ({params}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}"
    )
    written = 0
    with target.begin() as conn:
        for i in range(0, len(rows), _BATCH):
            chunk = [{c: r.get(c) for c in COLS} for r in rows[i:i + _BATCH]]
            conn.execute(upsert, chunk)
            written += len(chunk)
            print(f"[load_workplan_items] {written}/{len(rows)}")
    print(f"[load_workplan_items] upserted {written} workplan_items")

    # Post-upsert: renumber WBS codes for plans that had deduped duplicates.
    # The dedup step NULLed duplicate wbs_codes; the app's _renumber_wbs_codes
    # excludes NULL-WBS items from the tree, orphaning their children and
    # causing unique constraint collisions on later task create/delete.
    if _deduped_plans:
        _renumber_deduped_plans(target, _deduped_plans)

    return written


def _renumber_deduped_plans(target: Engine, plan_ids: set[str]) -> None:
    """Assign WBS codes to NULL-WBS items left by the dedup step.

    Builds the tree from parent_id + sort_order (same logic as the app's
    ``_renumber_wbs_codes``), computes new codes for every item, and
    applies changes via a two-step UPDATE (tmp- prefix then final) to
    avoid transient unique-constraint collisions.
    """
    with target.begin() as conn:
        total_fixed = 0
        for pid in plan_ids:
            items = conn.execute(text("""
                SELECT id::text, parent_id::text, wbs_code, sort_order
                FROM lep.workplan_items
                WHERE launch_plan_id = CAST(:pid AS uuid) AND is_deleted = false
            """), {"pid": pid}).fetchall()

            children_by_parent: dict[str | None, list] = defaultdict(list)
            for r in items:
                children_by_parent[r[1]].append({
                    "id": r[0], "wbs_code": r[2], "sort_order": r[3] or 0,
                })

            def _sort_key(item: dict) -> tuple:
                code = item["wbs_code"] or ""
                segs = [s for s in code.split(".") if s.isdigit()]
                return tuple(int(s) for s in segs) if segs else (item["sort_order"],)

            new_codes: dict[str, str] = {}

            def _assign(parent_id: str | None, prefix: str) -> None:
                for idx, item in enumerate(
                    sorted(children_by_parent.get(parent_id, []), key=_sort_key),
                    start=1,
                ):
                    code = f"{prefix}.{idx}" if prefix else str(idx)
                    new_codes[item["id"]] = code
                    _assign(item["id"], code)

            _assign(None, "")

            old_codes = {r[0]: r[2] for r in items}
            to_update = {
                iid: code for iid, code in new_codes.items()
                if old_codes.get(iid) != code
            }
            if not to_update:
                continue

            null_fixed = sum(1 for iid in to_update if old_codes[iid] is None)

            # Two-step to avoid unique constraint collisions
            for iid in to_update:
                conn.execute(text(
                    "UPDATE lep.workplan_items SET wbs_code = 'tmp-' || id::text "
                    "WHERE id = CAST(:id AS uuid)"
                ), {"id": iid})
            for iid, code in to_update.items():
                conn.execute(text(
                    "UPDATE lep.workplan_items SET wbs_code = :code "
                    "WHERE id = CAST(:id AS uuid)"
                ), {"id": iid, "code": code})

            total_fixed += null_fixed
        print(f"[load_workplan_items] renumbered {total_fixed} deduped NULL-WBS items across {len(plan_ids)} plans")


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    src = source_engine()
    load(src, target_engine(), ResolutionMaps.load(src), dry_run=args.dry_run)
