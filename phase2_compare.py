"""Phase 2+3 — per-plan entity matrix and field-level diffs.

For the 87 paired plans: rebuild the EXPECTED target rows from today's legacy
data using the migration's own transform code (vendored migration_ref), fetch
the ACTUAL LaunchPad rows, and diff.

Classification of every difference:
  EXACT      value matches after the documented transform
  EXPLAINED  difference produced by a designed rule (wbs renumber, injection
             milestone flag, membership dedup, ...)
  DRIFT      row/field changed in either system AFTER the migration ran
             (post-migration app activity or legacy edits) — not a migration gap
  GAP        unexplained difference — a real migration finding

Migration run window (from lep.migration_audit): 2026-09-19 03:04–03:07 UTC.
Read-only on both sides.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import common  # noqa: F401
from common import launchpad_conn, legacy_engine, q, qpg_dicts

from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.transforms import (canonical_country, det_id, normalize_function,
                                          plan_health_status, plan_status_from_overall,
                                          task_is_at_risk, task_status, user_id_for_email)
from scripts.migration.load_workplan_items import (_date_offset_key, _normalize_wbs,
                                                   _recommended_key)
from scripts.migration.load_plans import DATE_MAP

OUT = Path(__file__).resolve().parent / "out"
MIG_START = datetime(2026, 9, 19, 3, 4, 0, tzinfo=timezone.utc)
MIG_END = datetime(2026, 9, 19, 3, 7, 0, tzinfo=timezone.utc)
# The migration source was a DMS replica whose freshest row is 2026-08-31 01:25:46 UTC
# (max(updated_at) of migrated rows in target). Legacy edits after this never reached
# the replica, so they are STALE_SOURCE, not transform gaps.
SNAPSHOT_CUTOFF = datetime(2026, 8, 31, 1, 25, 47, tzinfo=timezone.utc)


def _d(v):
    """Normalize to date for comparison (SQL Server datetime vs PG date)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    return v


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _after_migration(ts) -> bool:
    if ts is None:
        return False
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts > MIG_END
    return False


def _after_snapshot(ts) -> bool:
    if ts is None:
        return False
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts > SNAPSHOT_CUTOFF
    return False


def main() -> None:
    ph1 = json.load(open(OUT / "phase1.json", encoding="utf-8"))
    paired_legacy = {p["det_plan_id"]: p for p in ph1["legacy_plans"]}
    tgt_plan_ids = [t["id"] for t in ph1["target_plans"]]
    plan_pairs = [(paired_legacy[t], t) for t in tgt_plan_ids if t in paired_legacy]
    legacy_ids = [paired_legacy[t]["legacy_id"] for t in tgt_plan_ids if t in paired_legacy]
    print(f"[phase2] comparing {len(plan_pairs)} paired plans")

    src = legacy_engine()
    maps = ResolutionMaps.load(src)
    lp = launchpad_conn()

    idlist = ", ".join(f"'{i}'" for i in legacy_ids)

    # ---------- legacy pulls (scoped to the 87 plans) ----------
    print("[phase2] pulling legacy entities ...")
    L = {}
    L["tasks"] = q(src, f'SELECT * FROM lep."Tasks" WHERE "ProjectID" IN ({idlist})')
    task_ids = [str(t["ID"]) for t in L["tasks"]]
    L["assignments"] = q(src, f'''SELECT a."ID", a."Task", a."Resource", a."IsOwner"
        FROM lep."Assignments" a
        JOIN lep."Tasks" t ON t."ID" = a."Task" WHERE t."ProjectID" IN ({idlist})''')
    L["dependencies"] = q(src, f'''SELECT d."ID", d."FromTask", d."ToTask" FROM lep."Dependencies" d
        JOIN lep."Tasks" t ON t."ID" = d."FromTask" WHERE t."ProjectID" IN ({idlist})''')
    L["taskfunctions"] = q(src, f'''SELECT f."ID", f."TaskId", f."FunctionId", f."ProjectId"
        FROM lep."TaskFunctions" f WHERE f."ProjectId" IN ({idlist})''')
    L["taskdocuments"] = q(src, f'''SELECT d."ID", d."TaskId" FROM lep."TaskDocuments" d
        JOIN lep."Tasks" t ON t."ID" = d."TaskId" WHERE t."ProjectID" IN ({idlist})''')
    L["projectresources"] = q(src, f'SELECT "Project", "Resource", "Active" FROM lep."ProjectResources" WHERE "Project" IN ({idlist})')
    L["secondaryleads"] = q(src, f'SELECT "Project", "Resource" FROM lep."SecondaryLaunchLeads" WHERE "Project" IN ({idlist})')
    # loader takes ALL notifications (drops only unresolved users) — do the same
    L["notifications"] = q(src, 'SELECT "ID", "AssignedTo", "CreatedDate" FROM lep."Notifications"')
    L["functions_src"] = q(src, 'SELECT "ID", "Name" FROM lep."Functions"')
    L["projects"] = q(src, f'SELECT * FROM lep."Projects" WHERE "ID" IN ({idlist})')

    # ---------- target pulls ----------
    print("[phase2] pulling LaunchPad entities ...")
    T = {}
    T["items"] = qpg_dicts(lp, """
        SELECT id::text, launch_plan_id::text, parent_id::text, wbs_code, title, status,
               start_date, due_date, completed_date, is_milestone, is_at_risk, is_private,
               is_hidden, is_deleted, is_mandatory, importance, recommended_start_key,
               recommended_end_key, sort_order, updated_at
        FROM lep.workplan_items""")
    T["assignments"] = qpg_dicts(lp, """
        SELECT a.id::text, a.workplan_item_id::text, u.email, a.is_primary
        FROM lep.workplan_item_assignments a JOIN lep.users u ON u.id = a.user_id""")
    T["dependencies"] = qpg_dicts(lp, "SELECT id::text, dependency_type, lag FROM lep.workplan_item_dependencies")
    T["functions"] = qpg_dicts(lp, "SELECT id::text, workplan_item_id::text, function_id::text FROM lep.workplan_item_functions")
    T["documents"] = qpg_dicts(lp, "SELECT id::text FROM lep.workplan_item_documents")
    T["comments"] = qpg_dicts(lp, "SELECT id::text, workplan_item_id::text, body, created_at, updated_at FROM lep.workplan_item_comments")
    T["memberships"] = qpg_dicts(lp, """
        SELECT m.id::text, m.launch_plan_id::text, u.email, r.code AS role_code, m.is_deleted, m.created_at
        FROM lep.plan_memberships m JOIN lep.users u ON u.id = m.user_id
        JOIN lep.roles r ON r.id = m.role_id""")
    T["countries"] = qpg_dicts(lp, """
        SELECT c.name, c.id::text, bu.name AS bu_name
        FROM lep.countries c JOIN lep.business_units bu ON bu.id = c.business_unit_id""")
    T["notifications"] = qpg_dicts(lp, "SELECT id::text, title, is_read FROM lep.notifications")
    T["injections"] = qpg_dicts(lp, "SELECT id::text, workplan_item_id::text FROM lep.workplan_item_injections")
    T["inj_countries"] = qpg_dicts(lp, "SELECT id::text, workplan_item_injection_id::text FROM lep.workplan_item_injection_countries")
    T["users"] = qpg_dicts(lp, "SELECT id::text, email, display_name, is_active FROM lep.users")
    T["plans"] = qpg_dicts(lp, "SELECT *, id::text AS id_text FROM lep.launch_plans")
    audit_per_plan = qpg_dicts(lp, """
        SELECT entity_type, count(*) AS n
        FROM lep.audit_logs GROUP BY entity_type""")

    # ---------- expected task rows ----------
    plan_by_det = {det_id("plan", lid): lid for lid in legacy_ids}
    tgt_plan_by_id = {p["id_text"]: p for p in T["plans"]}
    plan_clds = {p["id_text"]: p.get("commercial_launch_date") for p in T["plans"]
                 if p.get("commercial_launch_date")}
    plan_types = {p["id_text"]: p["plan_type"] for p in T["plans"]}

    expected_items = {}
    for t in L["tasks"]:
        pid = det_id("plan", str(t["ProjectID"]))
        rag = maps.entry_name(t.get("Status"))
        expected_items[det_id("task", str(t["ID"]))] = {
            "launch_plan_id": pid,
            "parent_id": det_id("task", str(t["ParentID"])) if t.get("ParentID") else None,
            "wbs_code": _normalize_wbs(t.get("WBS")),
            "title": t.get("Name") or "(untitled)",
            "status": task_status(maps.entry_name(t.get("LepStatus"))),
            "start_date": _d(t.get("StartDate")),
            "due_date": _d(t.get("EndDate")),
            "completed_date": _d(t.get("CompletedDate")),
            "is_milestone": bool(t.get("IsMilestone")),
            "is_at_risk": bool(t.get("IsAtRisk")) or task_is_at_risk(rag),
            "is_private": bool(t.get("IsPrivate")),
            "is_hidden": bool(t.get("IsHidden")),
            "is_mandatory": (t.get("OutlineLevel") or 0) in (1, 2),
            "importance": maps.entry_name(t.get("Importance")),
            "recommended_start_key": (_recommended_key(maps.entry_name(t.get("RecommendedStart")))
                                      or _date_offset_key(_d(t.get("StartDate")), plan_clds.get(pid))),
            "recommended_end_key": (_recommended_key(maps.entry_name(t.get("RecommendedEnd")))
                                    or _date_offset_key(_d(t.get("EndDate")), plan_clds.get(pid))),
            "_src_updated": t.get("LastUpdatedDate"),
            "_src_created": t.get("CreatedDate"),
            "_is_mce_activity": bool(t.get("IsMCEActivity")),
            "_assigned_countries": t.get("AssignedCountries"),
        }

    # injection sources — exact replication of load_injections._read_injections
    _SPLIT = re.compile(r"[;,|]")
    actual_items = {r["id"]: r for r in T["items"]}
    actual_item_ids = set(actual_items)
    countries_map = {c["name"].strip().lower(): c["id"] for c in T["countries"]}
    for c in T["countries"]:
        if c["bu_name"] == "IBU":
            countries_map[c["name"].strip().lower()] = c["id"]  # IBU overlay wins
    injection_sources = set()
    exp_injcountry = set()
    for tid, e in expected_items.items():
        ptype = plan_types.get(e["launch_plan_id"])
        if ptype not in ("global", "mce") or tid not in actual_item_ids:
            continue
        if not (e["is_milestone"] or e["_is_mce_activity"]) or not e["_assigned_countries"]:
            continue
        targets = set()
        for raw in _SPLIT.split(e["_assigned_countries"]):
            cid = countries_map.get((canonical_country(raw) or "").lower())
            if cid:
                targets.add(cid)
        if not targets:
            continue
        injection_sources.add(tid)
        inj_id = det_id("injection", tid)
        for cid in targets:
            exp_injcountry.add(det_id("injcountry", inj_id, cid))


    # ---------- task diff ----------
    TASK_FIELDS = ["wbs_code", "title", "status", "start_date", "due_date", "completed_date",
                   "is_milestone", "is_at_risk", "is_private", "is_hidden", "is_mandatory",
                   "importance", "recommended_start_key", "recommended_end_key", "parent_id"]
    task_missing, task_added, field_diffs = [], [], []
    diff_counter = Counter()
    for tid, e in expected_items.items():
        a = actual_items.get(tid)
        if a is None:
            task_missing.append({"id": tid, "plan": e["launch_plan_id"], "title": e["title"],
                                 "created_after_snapshot": _after_snapshot(e.get("_src_created")),
                                 "src_created": str(e.get("_src_created"))})
            continue
        for f in TASK_FIELDS:
            ev, av = e[f], a[f]
            if f in ("start_date", "due_date", "completed_date"):
                ev, av = _d(ev), _d(av)
            if ev == av:
                continue
            # classify
            if f == "is_milestone" and av is True and tid in injection_sources:
                cls = "EXPLAINED:injection_milestone"
            elif f == "wbs_code":
                cls = "EXPLAINED:wbs_renumber_candidate"
            elif _after_migration(a.get("updated_at")):
                cls = "DRIFT:target_updated_after_migration"
            elif _after_migration(e.get("_src_updated")):
                cls = "DRIFT:source_updated_after_migration"
            elif _after_snapshot(e.get("_src_updated")):
                cls = "STALE_SOURCE:legacy_edited_after_aug31_snapshot"
            else:
                cls = "GAP"
            diff_counter[(f, cls)] += 1
            if len(field_diffs) < 5000:
                field_diffs.append({"id": tid, "plan": e["launch_plan_id"], "field": f,
                                    "expected": ev, "actual": av, "class": cls,
                                    "title": e["title"][:60]})
    exp_ids = set(expected_items)
    for tid, a in actual_items.items():
        if tid not in exp_ids:
            task_added.append({"id": tid, "plan": a["launch_plan_id"], "title": a["title"],
                               "updated_at": a.get("updated_at")})

    # ---------- plan field diff ----------
    plan_diffs = []
    users_by_id = {u["id"]: u for u in T["users"]}
    for lpn, tid in plan_pairs:
        lrow = next(p for p in L["projects"] if str(p["ID"]) == lpn["legacy_id"])
        arow = tgt_plan_by_id[tid]
        overall = maps.entry_name(lrow.get("OverallLaunchStatus"))
        checks = {
            "name": (lrow.get("ProjectName"), arow.get("name")),
            "status": (plan_status_from_overall(overall) or "active", arow.get("status")),
            "health_status": (plan_health_status(overall), arow.get("health_status")),
            "is_deleted": (bool(lrow.get("IsDeleted")), arow.get("is_deleted")),
            "is_private": (bool(lrow.get("IsPrivate")), arow.get("is_private")),
            "executive_summary": (lrow.get("ExecutiveSummary"), arow.get("executive_summary")),
            "launch_lead_email": (maps.resource_email(lrow.get("Owner")) or None,
                                  (users_by_id.get(str(arow.get("launch_lead_id"))) or {}).get("email")),
        }
        for s_col, t_col in DATE_MAP.items():
            checks[t_col] = (_d(lrow.get(s_col)), _d(arow.get(t_col)))
        for f, (ev, av) in checks.items():
            if ev != av:
                if f == "executive_summary" and "platform will transition" in str(ev or "").lower():
                    # cutover banner bulk-posted into legacy plans after the snapshot
                    cls = "EXPLAINED:legacy_cutover_banner"
                elif _after_migration(arow.get("updated_at")):
                    cls = "DRIFT:target_updated_after_migration"
                elif _after_migration(lrow.get("LastUpdatedDate")):
                    cls = "DRIFT:source_updated_after_migration"
                elif _after_snapshot(lrow.get("LastUpdatedDate")):
                    cls = "STALE_SOURCE:legacy_edited_after_aug31_snapshot"
                else:
                    cls = "GAP"
                plan_diffs.append({"plan": tid, "name": lpn["name"], "field": f,
                                   "expected": ev, "actual": av, "class": cls})

    # ---------- entity presence (keyed) ----------
    def presence(label, expected_keys, actual_keys):
        missing = expected_keys - actual_keys
        added = actual_keys - expected_keys
        return {"entity": label, "expected": len(expected_keys), "actual": len(actual_keys),
                "matched": len(expected_keys & actual_keys),
                "missing": len(missing), "added": len(added),
                "missing_ids": sorted(missing)[:50], "added_ids": sorted(added)[:50]}

    valid_task_ids = {det_id("task", t) for t in task_ids} & set(actual_items)
    ent = []
    ent.append(presence("workplan_items", set(expected_items), set(actual_items)))

    # target user det-ids (the loaders' FK-safe filter drops users absent from target)
    tgt_user_det = {user_id_for_email(u["email"]) for u in T["users"] if u.get("email")}

    # assignments — natural key (item, user det-id); loader dedups (wi,uid) keep is_primary
    exp_asg = {}
    for a in L["assignments"]:
        wi = det_id("task", str(a["Task"]))
        if wi not in valid_task_ids:
            continue
        uid = maps.user_id(a["Resource"])
        if not uid or uid not in tgt_user_det:
            continue
        key = (wi, uid)
        prim = bool(a["IsOwner"])
        if key not in exp_asg or (prim and not exp_asg[key]):
            exp_asg[key] = prim
    act_asg = {(r["workplan_item_id"], user_id_for_email(r["email"])) for r in T["assignments"]}
    ent.append(presence("workplan_item_assignments (nat key)", set(exp_asg), act_asg))

    exp_dep = set()
    for d in L["dependencies"]:
        ft = det_id("task", str(d["FromTask"])); tt = det_id("task", str(d["ToTask"]))
        if ft in valid_task_ids and tt in valid_task_ids:
            exp_dep.add(det_id("dep", str(d["ID"])))
    ent.append(presence("workplan_item_dependencies", exp_dep, {r["id"] for r in T["dependencies"]}))

    # item functions — loader maps FunctionId -> Functions.Name -> plan's L1 item title
    l1_by_plan = {}
    for r in T["items"]:
        wbs = r.get("wbs_code")
        if wbs and "." not in wbs and not r["is_deleted"]:
            nf = normalize_function(r["title"])
            if nf:
                l1_by_plan[(r["launch_plan_id"], nf.lower())] = r["id"]
    src_fns = {str(f["ID"]): f["Name"] for f in L["functions_src"]}
    exp_fn = set()
    for f in L["taskfunctions"]:
        wi = det_id("task", str(f["TaskId"]))
        plan_id = det_id("plan", str(f["ProjectId"]))
        fname = normalize_function(src_fns.get(str(f["FunctionId"])))
        fid = l1_by_plan.get((plan_id, (fname or "").lower()))
        if wi in valid_task_ids and fid:
            exp_fn.add((wi, fid))
    act_fn = {(r["workplan_item_id"], r["function_id"]) for r in T["functions"]}
    ent.append(presence("workplan_item_functions (nat key)", exp_fn, act_fn))

    exp_doc = {det_id("doc", str(d["ID"])) for d in L["taskdocuments"]
               if det_id("task", str(d["TaskId"])) in valid_task_ids}
    ent.append(presence("workplan_item_documents", exp_doc, {r["id"] for r in T["documents"]}))

    exp_com = {det_id("comment", str(t["ID"])) for t in L["tasks"]
               if (t.get("Note") or "").strip() and det_id("task", str(t["ID"])) in valid_task_ids}
    ent.append(presence("workplan_item_comments", exp_com, {r["id"] for r in T["comments"]}))

    exp_notif = set()
    exp_notif_missing_post_snapshot = 0
    for n in L["notifications"]:
        uid = maps.user_id(n["AssignedTo"])
        if uid and uid in tgt_user_det:
            exp_notif.add((det_id("notif", str(n["ID"])), _after_snapshot(n.get("CreatedDate"))))
    act_notif = {r["id"] for r in T["notifications"]}
    notif_missing = {i for i, _ in exp_notif if i not in act_notif}
    exp_notif_missing_post_snapshot = sum(1 for i, post in exp_notif if i in notif_missing and post)
    ent.append({"entity": "notifications", "expected": len(exp_notif), "actual": len(act_notif),
                "matched": len({i for i, _ in exp_notif} & act_notif),
                "missing": len(notif_missing), "added": len(act_notif - {i for i, _ in exp_notif}),
                "missing_created_after_snapshot": exp_notif_missing_post_snapshot,
                "missing_ids": sorted(notif_missing)[:20], "added_ids": []})

    exp_inj = {det_id("injection", tid) for tid in injection_sources}
    ent.append(presence("workplan_item_injections", exp_inj, {r["id"] for r in T["injections"]}))
    ent.append(presence("workplan_item_injection_countries", exp_injcountry,
                        {r["id"] for r in T["inj_countries"]}))

    # memberships — exact loader replication (det membership id + is_deleted semantics)
    exp_mem = {}   # mid -> is_deleted
    seen_mid = set()
    mem_rows = []
    def _add_mem(plan_guid, res_guid, role_code, active=True):
        plan_id = det_id("plan", str(plan_guid))
        uid = maps.user_id(res_guid)
        if plan_id not in tgt_plan_by_id or not uid or uid not in tgt_user_det:
            return
        mid = det_id("membership", plan_id, uid, role_code)
        if mid in seen_mid:
            return
        seen_mid.add(mid)
        mem_rows.append({"mid": mid, "plan": plan_id, "uid": uid, "role": role_code,
                         "is_deleted": not bool(active)})
    for r in L["projectresources"]:
        _add_mem(r["Project"], r["Resource"], "team_member", r["Active"])
    for s in L["secondaryleads"]:
        _add_mem(s["Project"], s["Resource"], "secondary_launch_lead")
    sll_pairs = {(m["plan"], m["uid"]) for m in mem_rows if m["role"] == "secondary_launch_lead"}
    for m in mem_rows:
        if m["role"] == "team_member" and (m["plan"], m["uid"]) in sll_pairs:
            m["is_deleted"] = True
        exp_mem[m["mid"]] = m["is_deleted"]
    act_mem = {m["id"]: m for m in T["memberships"]}
    mem_missing = set(exp_mem) - set(act_mem)
    mem_added = set(act_mem) - set(exp_mem)
    mem_added_post = {m for m in mem_added if _after_migration(act_mem[m].get("created_at"))}
    mem_flag_mismatch = [m for m in set(exp_mem) & set(act_mem)
                         if exp_mem[m] != act_mem[m]["is_deleted"]]
    ent.append({"entity": "plan_memberships (det id)", "expected": len(exp_mem),
                "actual": len(act_mem), "matched": len(set(exp_mem) & set(act_mem)),
                "missing": len(mem_missing), "added": len(mem_added),
                "added_post_migration": len(mem_added_post),
                "is_deleted_flag_mismatches": len(mem_flag_mismatch),
                "missing_ids": sorted(mem_missing)[:50],
                "added_ids": sorted(mem_added - mem_added_post)[:50]})

    # users referenced
    lead_by_plan = {}
    for lpn, tid in plan_pairs:
        lrow = next(p for p in L["projects"] if str(p["ID"]) == lpn["legacy_id"])
        lead_by_plan[tid] = maps.resource_email(lrow.get("Owner"))
    referenced = {e for e in lead_by_plan.values() if e}
    tgt_emails = {u["email"] for u in T["users"]}
    missing_users = referenced - tgt_emails
    ent.append({"entity": "users (plan leads present)", "expected": len(referenced),
                "actual": len(tgt_emails), "matched": len(referenced & tgt_emails),
                "missing": len(missing_users), "added": None,
                "missing_ids": sorted(missing_users)[:20], "added_ids": []})

    # ---------- terminal output ----------
    W = 110
    print("\n" + "=" * W)
    print("PHASE 2 — ENTITY PRESENCE (deterministic-key join, expected-after-transform vs actual)")
    print("=" * W)
    print(f"  {'entity':42s} {'expected':>9s} {'actual':>9s} {'matched':>9s} {'missing':>8s} {'added':>7s}")
    for e in ent:
        print(f"  {e['entity']:42s} {e['expected']:>9,d} {e['actual']:>9,d} "
              f"{e['matched']:>9,d} {e['missing']:>8,d} {str(e['added']):>7s}")

    print("\n" + "=" * W)
    print("PHASE 3 — FIELD-LEVEL DIFFS (tasks: 15 fields x ~50k rows; plans: 27 fields x 87)")
    print("=" * W)
    print(f"  tasks missing in LaunchPad: {len(task_missing)}   tasks added in LaunchPad: {len(task_added)}")
    total = sum(diff_counter.values())
    print(f"  task field differences: {total}")
    for (f, cls), n in sorted(diff_counter.items(), key=lambda kv: -kv[1]):
        print(f"    {f:28s} {cls:44s} {n:>8,d}")
    print(f"\n  plan field differences: {len(plan_diffs)}")
    for pdiff in plan_diffs[:40]:
        print(f"    {pdiff['name'][:38]!r:40s} {pdiff['field']:28s} {pdiff['class']:12s} "
              f"exp={str(pdiff['expected'])[:30]!r} act={str(pdiff['actual'])[:30]!r}")

    json.dump({"entities": ent, "task_missing": task_missing, "task_added": task_added[:200],
               "task_field_diff_counter": {f"{f}|{c}": n for (f, c), n in diff_counter.items()},
               "task_field_diffs": field_diffs, "plan_diffs": plan_diffs,
               "audit_per_plan": audit_per_plan},
              open(OUT / "phase2.json", "w", encoding="utf-8"), default=_json_default)
    print(f"\n[phase2] wrote {OUT / 'phase2.json'}")


if __name__ == "__main__":
    sys.exit(main())
