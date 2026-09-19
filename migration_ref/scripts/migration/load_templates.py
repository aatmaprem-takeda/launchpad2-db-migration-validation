"""Load template assignments from ``data/template_mapping.csv``.

Creates ``lep.launch_templates`` records, populates
``lep.launch_template_applicabilities`` (BU / TA / brand / indication),
sets ``launch_plans.template_id`` on every migrated plan, **and migrates
WBS items from matching legacy template projects into
``lep.launch_template_wbs_items``**.

The mapping CSV has two columns:

    launch_plan_name,template

where *template* is either a UUID (used as-is for ``template_id``)
or a template name (converted to a deterministic uuid5 via ``det_id``).
Named templates are matched to legacy ``Projects`` where
``IsTemplate = true`` to pull their WBS hierarchy.

Idempotent: ``ON CONFLICT (id) DO UPDATE`` / ``DO NOTHING``.
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

from sqlalchemy import Engine, text

from scripts.migration.transforms import det_id

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

_MAPPING_PATH = Path(__file__).with_name("data") / "template_mapping.csv"

_ITEM_TYPE = {1: "function", 2: "activity"}  # depth -> item_type; default "task"


# ---------------------------------------------------------------------------
# Reference lookups
# ---------------------------------------------------------------------------

def _ref_ids(target: Engine):
    """Return dicts needed for business-function / framework inference and
    plan metadata resolution."""
    bf, fw = {}, {}
    with target.connect() as c:
        for r in c.execute(text("SELECT id::text, name, code FROM lep.business_functions")):
            bf[r.name.upper()] = r.id
            bf[r.code.upper()] = r.id   # also index by code
        for r in c.execute(text("SELECT id::text, name FROM lep.launch_frameworks")):
            fw[r.name.upper()] = r.id
    return {
        "bf_loc": bf.get("LOC") or bf.get("LOCAL"),
        "bf_mce": bf.get("MCE"),
        "bf_global": bf.get("GLOBAL"),
        "fw_local": fw.get("LOCAL LAUNCH FRAMEWORK"),
        "fw_mce": fw.get("MCE LAUNCH FRAMEWORK"),
        "fw_global": fw.get("GLOBAL LAUNCH FRAMEWORK"),
    }


def _plan_metadata(target: Engine) -> dict:
    """plan_name -> {bu_id, ta_id, brand_id, indication_id} for migrated plans."""
    meta = {}
    with target.connect() as c:
        for r in c.execute(text("""
            SELECT lp.name,
                   bu.id::text  AS bu_id,
                   ta.id::text  AS ta_id,
                   bm.id::text  AS brand_id,
                   ind.id::text AS indication_id
            FROM lep.launch_plans lp
            LEFT JOIN lep.business_units    bu  ON bu.id  = lp.business_unit_id
            LEFT JOIN lep.brand_molecules   bm  ON bm.id  = lp.brand_molecule_id
            LEFT JOIN lep.therapeutic_areas  ta  ON ta.id  = bm.therapeutic_area_id
            LEFT JOIN lep.indications        ind ON ind.id = lp.indication_id
            WHERE substring(lp.id::text, 15, 1) = '5'
              AND lp.is_deleted = false
        """)).mappings():
            meta[r["name"]] = dict(r)
    return meta


# ---------------------------------------------------------------------------
# Template inference
# ---------------------------------------------------------------------------

def _infer_function(tmpl_name: str, plan_names: list[str], refs: dict):
    """Return (business_function_id, framework_id) from the template name
    or the plan names it maps to."""
    low = tmpl_name.lower()
    if "mce" in low:
        return refs["bf_mce"], refs["fw_mce"]
    if "global" in low:
        return refs["bf_global"], refs["fw_global"]
    # Template name contains "loc" — LOC classification takes precedence
    # over any individual plan names (e.g. a Global plan may be mapped to
    # an LOC template when its own template is unavailable).
    if "loc" in low:
        return refs["bf_loc"], refs["fw_local"]
    for pn in plan_names:
        plow = pn.lower()
        if plow.startswith("global "):
            return refs["bf_global"], refs["fw_global"]
        if plow.startswith("mce "):
            return refs["bf_mce"], refs["fw_mce"]
    return refs["bf_loc"], refs["fw_local"]


def _template_id(tmpl_val: str) -> str:
    """UUID pass-through or deterministic uuid5."""
    return tmpl_val if _UUID_RE.match(tmpl_val) else det_id("template", tmpl_val)


def _template_name(tmpl_val: str, plan_names: list[str], bf_id: str, refs: dict) -> str:
    """Readable name: keep the original for named templates; infer for UUIDs."""
    if not _UUID_RE.match(tmpl_val):
        return tmpl_val
    brand_tok = "Unknown"
    for tok in ["TAK-121", "TAK-279", "TAK-861"]:
        if any(tok in p for p in plan_names):
            brand_tok = tok
            break
    func_label = (
        "LOC" if bf_id == refs["bf_loc"]
        else ("MCE" if bf_id == refs["bf_mce"] else "Global")
    )
    return f"{brand_tok} {func_label} Template ({tmpl_val[:8]})"


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def _read_mapping(path: Path | None = None) -> list[dict]:
    p = path or _MAPPING_PATH
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load(
    source: Engine,          # unused — kept for run_all signature consistency
    target: Engine,
    maps=None,               # unused
    *,
    dry_run: bool = False,
    _dry_state: dict | None = None,
    mapping_path: Path | None = None,
) -> int:
    mapping = _read_mapping(mapping_path)
    refs = _ref_ids(target)
    plan_meta = _plan_metadata(target)

    # ── group plans by template value ──
    tmpl_plans: dict[str, list[str]] = defaultdict(list)
    for row in mapping:
        tmpl_plans[row["template"].strip()].append(row["launch_plan_name"].strip())

    # ── Step 1: build template records ──
    templates: dict[str, dict] = {}  # template_id -> {name, bf, fw}
    for tmpl_val, plans in tmpl_plans.items():
        bf_id, fw_id = _infer_function(tmpl_val, plans, refs)
        tid = _template_id(tmpl_val)
        name = _template_name(tmpl_val, plans, bf_id, refs)
        templates[tid] = {"name": name, "bf": bf_id, "fw": fw_id}

    # ── Step 2: build applicability rows ──
    # Read valid BF→BU combinations to filter out invalid pairings
    # (e.g. LOC+SPD is invalid — SPD is only valid for Global).
    valid_bf_bu: set[tuple[str, str]] = set()
    with target.connect() as c:
        for r in c.execute(text("""
            SELECT business_function_id::text, business_unit_id::text
            FROM lep.business_function_business_units
            WHERE is_active = true
        """)):
            valid_bf_bu.add((r[0], r[1]))

    app_rows: list[dict] = []
    tmpl_combos: dict[str, set] = defaultdict(set)
    skipped_bu = 0
    for row in mapping:
        pname = row["launch_plan_name"].strip()
        tid = _template_id(row["template"].strip())
        pm = plan_meta.get(pname)
        if pm:
            bf_id = templates[tid]["bf"]  # template's business function
            bu_id = pm["bu_id"]
            # Only include BUs valid for this template's business function
            if valid_bf_bu and (bf_id, bu_id) not in valid_bf_bu:
                skipped_bu += 1
                continue
            key = (bu_id, pm["ta_id"], pm["brand_id"], pm["indication_id"])
            tmpl_combos[tid].add(key)

    for tid, combos in tmpl_combos.items():
        for bu_id, ta_id, brand_id, indication_id in combos:
            row_id = det_id("tmpl_app", tid, str(bu_id), str(brand_id), str(indication_id))
            app_rows.append({
                "id": row_id, "template_id": tid,
                "bu_id": bu_id, "ta_id": ta_id,
                "brand_id": brand_id, "indication_id": indication_id,
            })

    # ── Step 3: build plan_name -> template_id ──
    plan_tid = {r["launch_plan_name"].strip(): _template_id(r["template"].strip())
                for r in mapping}

    if dry_run:
        print(f"[load_templates] DRY RUN: {len(templates)} templates, "
              f"{len(app_rows)} applicability rows, "
              f"{len(plan_tid)} plan assignments — no write")
        return len(templates)

    # ── write: templates ──
    with target.begin() as c:
        for tid, info in templates.items():
            c.execute(text("""
                INSERT INTO lep.launch_templates
                    (id, name, business_function_id, framework_id,
                     version, template_status, is_active)
                VALUES (CAST(:id AS uuid), :name, CAST(:bf AS uuid),
                        CAST(:fw AS uuid), 1, 'active', true)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
            """), {"id": tid, "name": info["name"], "bf": info["bf"], "fw": info["fw"]})

    # ── write: applicabilities ──
    with target.begin() as c:
        for r in app_rows:
            c.execute(text("""
                INSERT INTO lep.launch_template_applicabilities
                    (id, launch_template_id, business_unit_id,
                     therapeutic_area_id, brand_molecule_id, indication_id,
                     priority, is_default, is_active)
                VALUES (CAST(:id AS uuid), CAST(:template_id AS uuid),
                        CAST(:bu_id AS uuid), CAST(:ta_id AS uuid),
                        CAST(:brand_id AS uuid), CAST(:indication_id AS uuid),
                        1, true, true)
                ON CONFLICT (id) DO NOTHING
            """), r)

    # ── write: plan template_id ──
    updated = 0
    not_found = []
    with target.begin() as c:
        for pname, tid in plan_tid.items():
            result = c.execute(text("""
                UPDATE lep.launch_plans
                SET template_id = CAST(:tid AS uuid), updated_at = now()
                WHERE name = :pname
                  AND substring(id::text, 15, 1) = '5'
                  AND is_deleted = false
            """), {"tid": tid, "pname": pname})
            if result.rowcount > 0:
                updated += result.rowcount
            else:
                not_found.append(pname)

    if skipped_bu:
        print(f"[load_templates] skipped {skipped_bu} applicability rows with invalid BF+BU combo")
    if not_found:
        print(f"[load_templates] WARNING {len(not_found)} plans not found: {not_found[:5]}")

    # ── write: WBS items from legacy template projects ──
    wbs_total = _load_wbs(source, target, templates)

    # ── seed framework-functions (idempotent — skips if already populated) ──
    _seed_framework_functions(target)

    # ── backfill level_definition_id on workplan_items for already-provisioned plans ──
    with target.begin() as c:
        wi_filled = c.execute(text("""
            UPDATE lep.workplan_items wi
            SET level_definition_id = ld.id
            FROM lep.launch_plans lp
            JOIN lep.launch_templates lt ON lt.id = lp.template_id
            JOIN lep.wbs_level_definitions ld ON ld.framework_id = lt.framework_id
            WHERE wi.launch_plan_id = lp.id
              AND wi.wbs_code IS NOT NULL
              AND wi.level_definition_id IS NULL
              AND ld.level_number = (
                  LENGTH(wi.wbs_code) - LENGTH(REPLACE(wi.wbs_code, '.', '')) + 1
              )
        """)).rowcount
    if wi_filled:
        print(f"[load_templates] backfilled level_definition_id on {wi_filled} workplan items")

    print(f"[load_templates] {len(templates)} templates, "
          f"{len(app_rows)} applicability rows, "
          f"{updated}/{len(plan_tid)} plans updated, "
          f"{wbs_total} WBS items")
    return len(templates)


# ---------------------------------------------------------------------------
# WBS items from legacy template Projects
# ---------------------------------------------------------------------------

def _legacy_template_projects(source: Engine) -> dict[str, str]:
    """Return {project_name: project_id} for IsTemplate=true projects."""
    with source.connect() as c:
        rows = c.execute(text("""
            SELECT "ID"::text, "ProjectName"
            FROM lep."Projects"
            WHERE COALESCE("IsTemplate", false) = true
        """)).fetchall()
    return {r[1].strip(): r[0] for r in rows}


def _load_wbs(source: Engine, target: Engine, templates: dict) -> int:
    """Load WBS items from legacy template projects into
    ``lep.launch_template_wbs_items``.  Returns total rows written."""
    legacy = _legacy_template_projects(source)

    # Match target template name -> legacy project_id
    tmpl_to_src: dict[str, str] = {}  # template_id -> source project_id
    for tid, info in templates.items():
        src_pid = legacy.get(info["name"])
        if src_pid:
            tmpl_to_src[tid] = src_pid

    if not tmpl_to_src:
        print("[load_templates] no legacy template projects matched — WBS skipped")
        return 0

    total = 0
    for tid, src_pid in tmpl_to_src.items():
        with source.connect() as c:
            tasks = c.execute(text("""
                SELECT "ID"::text, "ParentID"::text, "WBS", "Name",
                       "IsMilestone", "LepStatus", "Status", "Note",
                       "Importance", "IsHidden", "IsPrivate",
                       "IsTogglable", "IsDatesAutomationOn",
                       "RecommendedStart", "RecommendedEnd",
                       "Dependencies", "Considerations",
                       "CreatedDate", "LastUpdatedDate"
                FROM lep."Tasks"
                WHERE "ProjectID"::text = :pid
                ORDER BY "WBS"
            """), {"pid": src_pid}).mappings().fetchall()

        if not tasks:
            continue

        rows: list[dict] = []
        seen_wbs: set[str] = set()
        for idx, t in enumerate(tasks):
            wbs = t["WBS"] or ""
            if wbs and wbs in seen_wbs:
                continue  # skip duplicate WBS codes
            if wbs:
                seen_wbs.add(wbs)
            depth = wbs.count(".") + 1 if wbs else 1
            parent_src = t["ParentID"]
            rows.append({
                "id": det_id("tmpl_wbs", tid, t["ID"]),
                "launch_template_id": tid,
                "parent_id": det_id("tmpl_wbs", tid, parent_src) if parent_src else None,
                "wbs_code": wbs or None,
                "title": t["Name"] or "(untitled)",
                "item_type": _ITEM_TYPE.get(depth, "task"),
                "is_milestone": bool(t["IsMilestone"]) if t["IsMilestone"] is not None else False,
                "sort_order": idx + 1,
                "is_deleted": False,
                "description": t["Considerations"] or None,
                "source_status_text": t["LepStatus"],
                "source_risk_text": t["Status"],
                "source_comment": t["Note"],
                "source_dependency_text": t["Dependencies"],
                "source_type": "legacy",
                "importance": t["Importance"],
                "recommended_start_key": t["RecommendedStart"],
                "recommended_end_key": t["RecommendedEnd"],
                "toggle_enabled": bool(t["IsTogglable"]) if t["IsTogglable"] is not None else None,
                "is_dates_automation_enabled": bool(t["IsDatesAutomationOn"]) if t["IsDatesAutomationOn"] else False,
                "is_private": bool(t["IsPrivate"]) if t["IsPrivate"] is not None else False,
                "is_hidden": bool(t["IsHidden"]) if t["IsHidden"] is not None else False,
                "show_on_dashboard": False,
                "is_optional": False,
                "is_mandatory": depth <= 2,
                "country_assignment_scope": "none",
                "created_at": t["CreatedDate"] or t["LastUpdatedDate"],
                "updated_at": t["LastUpdatedDate"],
            })

        # Sort by WBS depth so parents are inserted before children
        # (self-referencing FK on parent_id)
        rows.sort(key=lambda r: (r["wbs_code"] or "").count("."))

        # Collect valid ids so we can null-out orphan parent_ids
        valid_ids = {r["id"] for r in rows}
        for r in rows:
            if r["parent_id"] and r["parent_id"] not in valid_ids:
                r["parent_id"] = None

        with target.begin() as c:
            for r in rows:
                c.execute(text("""
                    INSERT INTO lep.launch_template_wbs_items
                        (id, launch_template_id, parent_id, wbs_code, title,
                         item_type, is_milestone, sort_order, is_deleted,
                         description, source_status_text, source_risk_text,
                         source_comment, source_dependency_text, source_type,
                         importance, recommended_start_key, recommended_end_key,
                         toggle_enabled, is_dates_automation_enabled, is_private,
                         is_hidden, show_on_dashboard, is_optional, is_mandatory,
                         country_assignment_scope, created_at, updated_at)
                    VALUES (
                        CAST(:id AS uuid), CAST(:launch_template_id AS uuid),
                        CAST(:parent_id AS uuid), :wbs_code, :title,
                        :item_type, :is_milestone, :sort_order, :is_deleted,
                        :description, :source_status_text, :source_risk_text,
                        :source_comment, :source_dependency_text, :source_type,
                        :importance, :recommended_start_key, :recommended_end_key,
                        :toggle_enabled, :is_dates_automation_enabled, :is_private,
                        :is_hidden, :show_on_dashboard, :is_optional, :is_mandatory,
                        :country_assignment_scope, :created_at, :updated_at)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title, parent_id = EXCLUDED.parent_id,
                        wbs_code = EXCLUDED.wbs_code, item_type = EXCLUDED.item_type,
                        sort_order = EXCLUDED.sort_order
                """), r)

        total += len(rows)
        print(f"[load_templates]   {templates[tid]['name']}: {len(rows)} WBS items")

    print(f"[load_templates] WBS total: {total} items across {len(tmpl_to_src)} templates")

    # ── backfill level_definition_id (framework + WBS depth → level) ──
    with target.begin() as c:
        filled = c.execute(text("""
            UPDATE lep.launch_template_wbs_items tw
            SET level_definition_id = ld.id
            FROM lep.launch_templates lt
            JOIN lep.wbs_level_definitions ld ON ld.framework_id = lt.framework_id
            WHERE tw.launch_template_id = lt.id
              AND tw.wbs_code IS NOT NULL
              AND tw.level_definition_id IS NULL
              AND ld.level_number = (
                  LENGTH(tw.wbs_code) - LENGTH(REPLACE(tw.wbs_code, '.', '')) + 1
              )
        """)).rowcount
    if filled:
        print(f"[load_templates]   level_definition_id set on {filled} WBS items")

    # ── load launch_template_wbs_item_functions from legacy TaskFunctions ──
    wbs_funcs = _load_wbs_functions(source, target, templates, tmpl_to_src)
    if wbs_funcs:
        print(f"[load_templates]   {wbs_funcs} template WBS item-function assignments")

    return total


# ---------------------------------------------------------------------------
# WBS item → function assignments from legacy TaskFunctions
# ---------------------------------------------------------------------------

def _load_wbs_functions(
    source: Engine, target: Engine,
    templates: dict, tmpl_to_src: dict[str, str],
) -> int:
    """Populate ``launch_template_wbs_item_functions`` by mapping legacy
    ``TaskFunctions`` entries to L1 WBS items (the "function" level) in each
    template, matched by function name."""
    import uuid as _uuid

    NS = _uuid.UUID("6b1d1f2e-0000-4c00-8000-1e9000000000")

    # Legacy Functions lookup: ID -> Name
    with source.connect() as c:
        legacy_funcs: dict[str, str] = {}  # upper(ID) -> Name
        for r in c.execute(text('SELECT "ID"::text, "Name" FROM lep."Functions"')):
            legacy_funcs[r[0].strip().upper()] = r[1].strip()

    if not legacy_funcs:
        return 0

    # Target: L1 WBS items per template (title.lower() -> wbs_item_id)
    l1_items: dict[str, dict[str, str]] = defaultdict(dict)
    with target.connect() as c:
        for r in c.execute(text("""
            SELECT id::text, launch_template_id::text, wbs_code, title
            FROM lep.launch_template_wbs_items
            WHERE is_deleted = false AND wbs_code IS NOT NULL
        """)):
            wbs = r[2]
            if wbs and "." not in wbs:  # depth 1
                l1_items[r[1]][r[3].strip().lower()] = r[0]

    # Existing WBS item IDs (for FK validation)
    existing_wbs: set[str] = set()
    with target.connect() as c:
        for r in c.execute(text("SELECT id::text FROM lep.launch_template_wbs_items")):
            existing_wbs.add(r[0])

    def _find_l1(func_name: str, l1_dict: dict[str, str]) -> str | None:
        fn = func_name.lower()
        if fn in l1_dict:
            return l1_dict[fn]
        for title, wbs_id in l1_dict.items():
            if fn in title or title in fn:
                return wbs_id
        return None

    total = 0
    for tid, src_pid in tmpl_to_src.items():
        tl1 = l1_items.get(tid, {})
        if not tl1:
            continue

        with source.connect() as c:
            tfs = c.execute(text("""
                SELECT "TaskId"::text, "FunctionId"::text
                FROM lep."TaskFunctions"
                WHERE "ProjectId"::text = :pid
            """), {"pid": src_pid}).fetchall()

        rows = []
        for task_id, func_id in tfs:
            func_name = legacy_funcs.get(func_id.strip().upper())
            if not func_name:
                continue
            l1_id = _find_l1(func_name, tl1)
            if not l1_id:
                continue
            wbs_item_id = str(det_id("tmpl_wbs", tid, task_id))
            if wbs_item_id not in existing_wbs:
                continue
            row_id = str(_uuid.uuid5(NS, f"twif:{wbs_item_id}:{l1_id}"))
            rows.append({"id": row_id, "wbs_item_id": wbs_item_id, "function_id": l1_id})

        if rows:
            with target.begin() as c:
                for r in rows:
                    c.execute(text("""
                        INSERT INTO lep.launch_template_wbs_item_functions
                            (id, launch_template_wbs_item_id, function_id,
                             is_deleted, created_at, updated_at)
                        VALUES (CAST(:id AS uuid), CAST(:wbs_item_id AS uuid),
                                CAST(:function_id AS uuid), false, now(), now())
                        ON CONFLICT (id) DO NOTHING
                    """), r)
            total += len(rows)

    return total


# ---------------------------------------------------------------------------
# Seed launch_framework_functions (all frameworks × all functions)
# ---------------------------------------------------------------------------

def _seed_framework_functions(target: Engine) -> int:
    """Ensure every active function is linked to every framework in
    ``launch_framework_functions``."""
    import uuid as _uuid

    NS = _uuid.UUID("6b1d1f2e-0000-4c00-8000-1e9000000000")

    with target.connect() as c:
        existing = c.execute(text(
            "SELECT count(*) FROM lep.launch_framework_functions"
        )).scalar()
        if existing > 0:
            return 0  # already seeded

        fws = c.execute(text(
            "SELECT id::text, name FROM lep.launch_frameworks"
        )).fetchall()
        funcs = c.execute(text(
            "SELECT id::text, name FROM lep.functions WHERE is_active = true"
        )).fetchall()

    rows = []
    for fw_id, _ in fws:
        for idx, (fn_id, _) in enumerate(funcs, 1):
            row_id = str(_uuid.uuid5(NS, f"lff:{fw_id}:{fn_id}"))
            rows.append({"id": row_id, "fw": fw_id, "fn": fn_id, "sort": idx})

    with target.begin() as c:
        for r in rows:
            c.execute(text("""
                INSERT INTO lep.launch_framework_functions
                    (id, framework_id, function_id, sort_order,
                     is_required, is_deleted, created_at, updated_at)
                VALUES (CAST(:id AS uuid), CAST(:fw AS uuid),
                        CAST(:fn AS uuid), :sort,
                        false, false, now(), now())
                ON CONFLICT (id) DO NOTHING
            """), r)

    print(f"[load_templates] seeded {len(rows)} launch_framework_functions")
    return len(rows)


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load(source_engine(), target_engine(), dry_run=args.dry_run)
