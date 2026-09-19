"""Phase 1 — scope & count matrix: LEP prod vs LaunchPad prod.

Pairs every LaunchPad launch plan with its legacy project through the
migration's own deterministic ID (uuid5('plan', legacy ID)), classifies every
legacy project against the migration scope (scope.py brand tokens), and prints
the count matrices. Writes out/phase1.json for later phases.

Read-only on both sides.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import common  # noqa: F401  (sets sys.path)
from common import launchpad_conn, legacy_engine, q, qpg_dicts

from scripts.migration import scope
from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.transforms import classify_plan_type, det_id

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def main() -> None:
    src = legacy_engine()
    print("[phase1] loading resolution maps from legacy prod ...")
    maps = ResolutionMaps.load(src)
    print(f"[phase1] {maps.summary()}")

    legacy = q(src, 'SELECT * FROM lep."Projects"')
    print(f"[phase1] legacy lep.Projects rows: {len(legacy)}")

    plans = []
    for p in legacy:
        fbi = maps.entry_name(p.get("FranchiseBrandIndication"))
        ptype = classify_plan_type(p.get("IsTemplate"), p.get("IsGlobalPlan"), p.get("IsMCEPlan"))
        plans.append(
            {
                "legacy_id": str(p["ID"]),
                "det_plan_id": det_id("plan", str(p["ID"])),
                "det_template_id": det_id("template", str(p["ID"])),
                "name": p.get("ProjectName"),
                "fbi": fbi,
                "plan_type": ptype,
                "is_template": bool(p.get("IsTemplate")),
                "is_deleted": bool(p.get("IsDeleted")),
                "in_scope": scope.in_scope(fbi),
                "region_country": maps.entry_name(p.get("RegionCountry")),
                "overall_status": maps.entry_name(p.get("OverallLaunchStatus")),
                "owner_email": maps.resource_email(p.get("Owner")),
            }
        )

    lp = launchpad_conn()
    tgt_plans = qpg_dicts(
        lp,
        """SELECT id::text, name, plan_type, status, health_status, is_deleted,
                  business_unit_id::text, brand_molecule_id::text, country_id::text,
                  launch_lead_id::text, created_at, updated_at
           FROM lep.launch_plans""",
    )
    tgt_templates = qpg_dicts(lp, "SELECT id::text, name FROM lep.launch_templates")

    # entity totals on target
    entity_tables = [
        "users", "functions", "launch_plans", "launch_templates", "workplan_items",
        "workplan_item_assignments", "workplan_item_dependencies", "workplan_item_functions",
        "workplan_item_tags", "workplan_item_documents", "workplan_item_comments",
        "workplan_item_country_assignments", "workplan_item_injections",
        "workplan_item_injection_countries", "plan_memberships", "trackers",
        "notifications", "audit_logs",
    ]
    tgt_counts = {}
    for t in entity_tables:
        tgt_counts[t] = qpg_dicts(lp, f"SELECT count(*) AS n FROM lep.{t}")[0]["n"]

    audit = qpg_dicts(
        lp,
        """SELECT seq, stage, target_table, rows_written, status
           FROM lep.migration_audit WHERE kind='stage' ORDER BY seq""",
    )

    # --- pairing ---
    by_det = {r["det_plan_id"]: r for r in plans}
    tpl_by_det = {r["det_template_id"]: r for r in plans}
    paired, unmatched_tgt = [], []
    for t in tgt_plans:
        srcp = by_det.get(t["id"])
        (paired if srcp else unmatched_tgt).append({"target": t, "legacy": srcp})

    matched_legacy_ids = {p["legacy"]["legacy_id"] for p in paired}
    inscope_nontpl = [p for p in plans if p["in_scope"] and not p["is_template"]]
    missing = [p for p in inscope_nontpl if p["legacy_id"] not in matched_legacy_ids]

    tpl_paired = sum(1 for t in tgt_templates if t["id"] in tpl_by_det)

    # --- matrices ---
    W = 100
    print("\n" + "=" * W)
    print("PHASE 1 — SCOPE & PLAN PAIRING (LEP prod ⟷ LaunchPad prod)")
    print("=" * W)
    c = Counter()
    for p in plans:
        c["total"] += 1
        if p["is_template"]:
            c["templates"] += 1
        elif p["is_deleted"]:
            c["deleted_nontpl"] += 1
        if p["in_scope"] and not p["is_template"]:
            c["in_scope_nontpl"] += 1
            if p["is_deleted"]:
                c["in_scope_deleted"] += 1
    print(f"  Legacy lep.Projects total ............ {c['total']}")
    print(f"    templates (IsTemplate=1) ........... {c['templates']}")
    print(f"    non-template, deleted flag ......... {c['deleted_nontpl']}")
    print(f"    IN SCOPE (3 brands, non-template) .. {c['in_scope_nontpl']}"
          f"  (of which deleted-flagged: {c['in_scope_deleted']})")
    print(f"  LaunchPad launch_plans ............... {len(tgt_plans)}")
    print(f"    paired to a legacy project ......... {len(paired)}")
    print(f"    UNMATCHED in target (app-created?) . {len(unmatched_tgt)}")
    print(f"  In-scope legacy plans MISSING in LP .. {len(missing)}")
    print(f"  LaunchPad launch_templates ........... {len(tgt_templates)} "
          f"(paired to legacy template projects: {tpl_paired})")

    brand_counts = Counter()
    for p in inscope_nontpl:
        for b in scope.MIGRATION_BRANDS:
            if p["fbi"] and any(tok in p["fbi"].lower() for tok in b["tokens"]):
                brand_counts[b["label"]] += 1
                break
    print("\n  In-scope legacy plans by brand:")
    for label, n in brand_counts.items():
        print(f"    {label:60s} {n}")

    print("\n" + "-" * W)
    print("ENTITY TOTALS — LaunchPad prod vs migration-audit rows_written")
    print("-" * W)
    print(f"  {'stage':18s} {'target table':38s} {'audit wrote':>12s} {'in DB now':>12s}")
    audit_by_table = {a["target_table"]: a for a in audit}
    for t in entity_tables:
        a = audit_by_table.get(t)
        wrote = a["rows_written"] if a else ""
        print(f"  {(a['stage'] if a else ''):18s} {t:38s} {str(wrote):>12s} {tgt_counts[t]:>12,d}")

    if missing:
        print("\n  MISSING in-scope plans (legacy -> absent in LaunchPad):")
        for p in missing[:20]:
            print(f"    {p['legacy_id']}  {p['name']!r:60s} deleted={p['is_deleted']} fbi={p['fbi']}")

    if unmatched_tgt:
        print("\n  Target plans with no legacy counterpart:")
        for u in unmatched_tgt[:20]:
            print(f"    {u['target']['id']}  {u['target']['name']!r}")

    json.dump(
        {
            "legacy_plans": plans,
            "target_plans": tgt_plans,
            "target_templates": tgt_templates,
            "target_counts": tgt_counts,
            "audit": audit,
            "missing_in_scope": missing,
            "unmatched_target": [u["target"] for u in unmatched_tgt],
        },
        open(OUT / "phase1.json", "w", encoding="utf-8"),
        default=_json_default,
    )
    print(f"\n[phase1] wrote {OUT / 'phase1.json'}")


if __name__ == "__main__":
    sys.exit(main())
