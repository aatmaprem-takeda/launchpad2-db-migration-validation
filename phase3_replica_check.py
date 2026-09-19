"""Phase 3 — replica -> target validation against the migration notebook.

The production migration (LEP Migration Lakebase notebook, run-20260919T120011Z)
read the DMS replica (Lakebase db "launchpad", schema lep, legacy tables) and
wrote databricks_postgres. This phase validates that path directly, plus the
NOTEBOOK-ONLY transformations phase 2 never covered:

  N1  runbook verification query + expected counts   (runbook cell 1)
  N2  level_definition_id assignment by WBS depth    (cell 40)
  N3  is_mandatory=true on L1/L2                     (cell 41)
  N4  completed_date backfill for completed items    (cell 41)
  N5  document url/name backfill from TaskDocuments  (cell 42)
  N6  recommended_start/end_key mapping + fallback   (cells 44/45)
  N7  injection countries always IBU country ids     (2026-09-18 DB fix)

Replica quirks handled: GUIDs stored UPPERCASE (det_id lowercases), quoted
mixed-case identifiers, booleans are true booleans.

Read-only on both databases (default_transaction_read_only=on, SELECT guard).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import common  # noqa: F401  (sys.path setup + SELECT guard)
from common import assert_select_only, launchpad_conn

import psycopg
from launchpad_prod_db import get_token, load_secure_env

from scripts.migration.transforms import det_id

OUT = Path(__file__).resolve().parent / "out"


def replica_conn() -> psycopg.Connection:
    """Read-only connection to the DMS replica db "launchpad" (same instance)."""
    env = load_secure_env()
    conn = psycopg.connect(
        host=env["LAUNCHPAD_PROD_DATABASE_HOST"],
        port=int(env.get("LAUNCHPAD_PROD_DATABASE_PORT", "5432")),
        dbname="launchpad",  # the replica db, NOT databricks_postgres
        user=env["LAUNCHPAD_PROD_DATABASE_USER"],
        password=get_token(env),
        sslmode=env.get("LAUNCHPAD_PROD_DATABASE_SSLMODE", "require"),
        connect_timeout=20,
        options="-c default_transaction_read_only=on",
    )
    with conn.cursor() as cur:
        cur.execute("SHOW default_transaction_read_only")
        assert cur.fetchone()[0] == "on", "replica session is not read-only"
    return conn


def q(conn, sql: str, params=None) -> list[tuple]:
    assert_select_only(sql)
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def main() -> None:
    ph1 = json.load(open(OUT / "phase1.json", encoding="utf-8"))
    in_scope = [p for p in ph1["legacy_plans"] if p["det_plan_id"] in {t["id"] for t in ph1["target_plans"]}]
    legacy_ids_uc = [p["legacy_id"].upper() for p in in_scope]
    print(f"[phase3] {len(in_scope)} in-scope plans; source = replica db 'launchpad'")

    rep = replica_conn()
    tgt = launchpad_conn()
    idlist = ", ".join(f"'{i}'" for i in legacy_ids_uc)
    results: dict = {"generated_at": datetime.now().isoformat(), "sections": {}}

    # ---------------- A. scoped counts: replica vs target ----------------
    print("\n=== A. Replica (migration source) vs target — scoped counts ===")
    a_rows = []

    def _one(name, rep_sql, tgt_sql):
        rc = q(rep, rep_sql)[0][0]
        tc = q(tgt, tgt_sql)[0][0]
        a_rows.append({"entity": name, "replica": rc, "target": tc, "delta": tc - rc})
        print(f"  {name:<28} replica={rc:>7,}  target={tc:>7,}  delta={tc - rc:+d}")

    mig = "substring(id::text, 15, 1) = '5'"
    _one("plans (Projects)",
         f'SELECT count(*) FROM lep."Projects" WHERE "ID" IN ({idlist})',
         f"SELECT count(*) FROM lep.launch_plans WHERE {mig}")
    _one("tasks (Tasks)",
         f'SELECT count(*) FROM lep."Tasks" WHERE "ProjectID" IN ({idlist})',
         f"SELECT count(*) FROM lep.workplan_items WHERE {mig}")
    _one("assignments",
         f'''SELECT count(*) FROM (SELECT DISTINCT a."Task", a."Resource"
             FROM lep."Assignments" a JOIN lep."Tasks" t ON t."ID" = a."Task"
             WHERE t."ProjectID" IN ({idlist})) x''',
         f"SELECT count(*) FROM lep.workplan_item_assignments WHERE {mig}")
    _one("dependencies",
         f'''SELECT count(*) FROM lep."Dependencies" d
             JOIN lep."Tasks" t ON t."ID" = d."FromTask" WHERE t."ProjectID" IN ({idlist})''',
         f"SELECT count(*) FROM lep.workplan_item_dependencies WHERE {mig}")
    _one("task-function links",
         f'SELECT count(*) FROM lep."TaskFunctions" WHERE "ProjectId" IN ({idlist})',
         f"SELECT count(*) FROM lep.workplan_item_functions WHERE {mig}")
    _one("documents",
         f'''SELECT count(*) FROM lep."TaskDocuments" d
             JOIN lep."Tasks" t ON t."ID" = d."TaskId" WHERE t."ProjectID" IN ({idlist})''',
         f"SELECT count(*) FROM lep.workplan_item_documents WHERE {mig}")
    _one("comments (Tasks.Note)",
         f'SELECT count(*) FROM lep."Tasks" WHERE "ProjectID" IN ({idlist}) '
         "AND \"Note\" IS NOT NULL AND btrim(\"Note\") <> ''",
         f"SELECT count(*) FROM lep.workplan_item_comments WHERE {mig}")
    results["sections"]["scoped_counts"] = a_rows

    # ------------- B. deterministic-ID presence from the replica -------------
    print("\n=== B. Deterministic-ID presence (replica keys -> target) ===")
    b = {}

    rep_task_ids = {r[0] for r in q(rep, f'SELECT "ID"::text FROM lep."Tasks" WHERE "ProjectID" IN ({idlist})')}
    exp_task = {det_id("task", t.lower()) for t in rep_task_ids}
    act_task = {r[0] for r in q(tgt, f"SELECT id::text FROM lep.workplan_items WHERE {mig}")}
    b["tasks"] = {"expected": len(exp_task), "actual": len(act_task),
                  "missing": len(exp_task - act_task), "extra": len(act_task - exp_task)}

    rep_doc_ids = {r[0] for r in q(rep, f'''SELECT d."ID"::text FROM lep."TaskDocuments" d
        JOIN lep."Tasks" t ON t."ID" = d."TaskId" WHERE t."ProjectID" IN ({idlist})''')}
    exp_doc = {det_id("doc", d.lower()) for d in rep_doc_ids}
    act_doc = {r[0] for r in q(tgt, f"SELECT id::text FROM lep.workplan_item_documents WHERE {mig}")}
    b["documents"] = {"expected": len(exp_doc), "actual": len(act_doc),
                      "missing": len(exp_doc - act_doc), "extra": len(act_doc - exp_doc)}

    exp_plan = {det_id("plan", g.lower()) for g in legacy_ids_uc}
    act_plan = {r[0] for r in q(tgt, f"SELECT id::text FROM lep.launch_plans WHERE {mig}")}
    b["plans"] = {"expected": len(exp_plan), "actual": len(act_plan),
                  "missing": len(exp_plan - act_plan), "extra": len(act_plan - exp_plan)}

    for k, v in b.items():
        flag = "PASS" if v["missing"] == 0 and v["extra"] == 0 else "CHECK"
        print(f"  {k:<12} expected={v['expected']:>7,} actual={v['actual']:>7,} "
              f"missing={v['missing']} extra={v['extra']}  [{flag}]")
    results["sections"]["det_presence"] = b

    # ------------- N1. runbook verification query vs expected -------------
    print("\n=== N1. Runbook verification queries (notebook cell 1) ===")
    ver_sql = f"""
        SELECT 'launch_frameworks' AS m, count(*) FROM lep.launch_frameworks
        UNION ALL SELECT 'wbs_level_definitions', count(*) FROM lep.wbs_level_definitions
        UNION ALL SELECT 'launch_framework_functions', count(*) FROM lep.launch_framework_functions
        UNION ALL SELECT 'launch_templates (fw set)', count(*) FROM lep.launch_templates WHERE framework_id IS NOT NULL
        UNION ALL SELECT 'plans with template_id', count(*) FROM lep.launch_plans WHERE template_id IS NOT NULL AND {mig}
        UNION ALL SELECT 'level_def SET', count(*) FROM lep.workplan_items WHERE level_definition_id IS NOT NULL AND {mig}
        UNION ALL SELECT 'level_def NULL', count(*) FROM lep.workplan_items WHERE level_definition_id IS NULL AND {mig}
        UNION ALL SELECT 'framework_fn SET', count(*) FROM lep.workplan_items WHERE framework_function_id IS NOT NULL AND {mig}
        UNION ALL SELECT 'item_functions', count(*) FROM lep.workplan_item_functions WHERE {mig}
        UNION ALL SELECT 'is_at_risk', count(*) FROM lep.workplan_items WHERE is_at_risk = true AND {mig}
        UNION ALL SELECT 'is_milestone', count(*) FROM lep.workplan_items WHERE is_milestone = true AND {mig}
        UNION ALL SELECT 'importance', count(*) FROM lep.workplan_items WHERE importance IS NOT NULL AND {mig}
    """
    expected = {  # runbook EXPECTED COUNTS (3-brand scope); None = informational
        "launch_frameworks": (3, 3), "wbs_level_definitions": (12, 12),
        "launch_framework_functions": (93, 93), "launch_templates (fw set)": (6, 6),
        "plans with template_id": (82, 82), "level_def NULL": (0, 0),
        "level_def SET": (49000, 51000), "framework_fn SET": (700, 900),
        "item_functions": (10500, 11500), "is_at_risk": (60, 130),
        "is_milestone": (180, 300), "importance": (1800, 2200),
    }
    n1 = []
    for metric, cnt in q(tgt, ver_sql):
        lo_hi = expected.get(metric)
        status = "INFO"
        if lo_hi:
            status = "PASS" if lo_hi[0] <= cnt <= lo_hi[1] else "FAIL"
        n1.append({"metric": metric, "count": cnt, "status": status})
        print(f"  {metric:<28} {cnt:>8,}  [{status}]")
    results["sections"]["runbook_verification"] = n1

    # ------------- N2. level_definition_id matches WBS depth -------------
    print("\n=== N2. level_definition_id consistent with WBS depth ===")
    rows = q(tgt, f"""
        SELECT count(*) FILTER (WHERE wld.level_number IS DISTINCT FROM
                 least(array_length(string_to_array(wi.wbs_code, '.'), 1), 4)
                 AND wi.wbs_code IS NOT NULL) AS mismatched,
               count(*) FILTER (WHERE wi.wbs_code IS NULL) AS no_wbs,
               count(*) AS total
        FROM lep.workplan_items wi
        LEFT JOIN lep.wbs_level_definitions wld ON wld.id = wi.level_definition_id
        WHERE substring(wi.id::text, 15, 1) = '5' AND wi.is_deleted = false""")
    mismatched, no_wbs, total = rows[0]
    print(f"  depth-vs-level mismatches: {mismatched} of {total:,} (no wbs: {no_wbs})")
    results["sections"]["level_def_consistency"] = {
        "mismatched": mismatched, "no_wbs": no_wbs, "total": total}

    # ------------- N3/N4. is_mandatory + completed_date -------------
    print("\n=== N3/N4. is_mandatory L1/L2 + completed_date backfill ===")
    m = q(tgt, f"""
        SELECT count(*) FILTER (WHERE wld.level_number IN (1,2) AND wi.is_mandatory = true),
               count(*) FILTER (WHERE wld.level_number IN (1,2)),
               count(*) FILTER (WHERE wi.status = 'completed' AND wi.completed_date IS NOT NULL),
               count(*) FILTER (WHERE wi.status = 'completed')
        FROM lep.workplan_items wi
        LEFT JOIN lep.wbs_level_definitions wld ON wld.id = wi.level_definition_id
        WHERE substring(wi.id::text, 15, 1) = '5' AND wi.is_deleted = false""")[0]
    print(f"  is_mandatory:   {m[0]:,}/{m[1]:,} L1+L2 items")
    print(f"  completed_date: {m[2]:,}/{m[3]:,} completed items")
    results["sections"]["mandatory_completed"] = {
        "mandatory_ok": m[0], "l1l2_total": m[1],
        "completed_date_ok": m[2], "completed_total": m[3]}

    # ------------- N5. document url/name backfill vs replica -------------
    print("\n=== N5. Document url/name vs replica TaskDocuments ===")
    rep_docs = q(rep, f'''SELECT d."ID"::text, d."Name", d."Description", d."Link"
        FROM lep."TaskDocuments" d
        JOIN lep."Tasks" t ON t."ID" = d."TaskId" WHERE t."ProjectID" IN ({idlist})''')
    tgt_docs = {r[0]: (r[1], r[2]) for r in q(tgt,
        f"SELECT id::text, url, name FROM lep.workplan_item_documents WHERE {mig}")}
    n5 = Counter()
    n5_samples = []
    for doc_id, name, desc, link in rep_docs:
        tid = det_id("doc", doc_id.lower())
        if tid not in tgt_docs:
            n5["missing"] += 1
            continue
        exp_url = (link or "").strip() or None
        exp_name = name or desc or "document"
        act_url, act_name = tgt_docs[tid]
        # cell 42: url = COALESCE(:url, url) — sharepoint re-upload (cell 43) may
        # have replaced legacy URLs with new-site URLs; count separately.
        if exp_url == act_url:
            n5["url_exact"] += 1
        elif act_url and "sharepoint" in (act_url or "").lower() and exp_url:
            n5["url_replaced_sharepoint"] += 1
        elif exp_url is None and act_url is None:
            n5["url_exact"] += 1
        else:
            n5["url_diff"] += 1
            if len(n5_samples) < 5:
                n5_samples.append({"doc": tid, "expected": exp_url, "actual": act_url})
        n5["name_match" if (exp_name or "").strip() == (act_name or "").strip() else "name_diff"] += 1
    print(f"  {dict(n5)}")
    for s in n5_samples:
        print(f"    diff: {s['doc'][:8]} exp={str(s['expected'])[:60]} act={str(s['actual'])[:60]}")
    results["sections"]["documents"] = {"counter": dict(n5), "samples": n5_samples}

    # ------------- N6. recommended keys: mapping + format -------------
    print("\n=== N6. recommended_start/end_key (cells 44/45) ===")
    fmt = re.compile(r"^L[+-]\d+[dmy]$")
    keys = q(tgt, f"""SELECT recommended_start_key, recommended_end_key
                      FROM lep.workplan_items WHERE {mig} AND is_deleted = false""")
    bad_fmt = sum(1 for sk, ek in keys for v in (sk, ek) if v and not fmt.match(v))
    cov_s = sum(1 for sk, _ in keys if sk)
    cov_e = sum(1 for _, ek in keys if ek)
    print(f"  coverage: start={cov_s:,}/{len(keys):,}  end={cov_e:,}/{len(keys):,}  bad-format={bad_fmt}")

    # recompute the LookupEntries-mapped keys from the replica and verify
    off_re = re.compile(r"^([+-]?\d+)([dmy])?$", re.IGNORECASE)

    def to_app_key(raw):
        if not raw:
            return None
        mm = off_re.match(raw.strip())
        if not mm:
            return None
        amount = mm.group(1)
        unit = (mm.group(2) or "m").lower()
        if not amount.startswith(("+", "-")):
            amount = "+" + amount
        return f"L{amount}{unit}"

    guid_to_key = {}
    for eid, ename in q(rep, '''SELECT le."ID"::text, le."Name"
            FROM lep."LookupEntries" le JOIN lep."Lookups" lk ON lk."ID" = le."Lookup"
            WHERE lk."Name" LIKE %s''', ("%Recommended%",)):
        k = to_app_key(ename)
        if k:
            guid_to_key[eid.replace("-", "").upper()] = k

    rep_rec = q(rep, f'''SELECT t."ID"::text, t."RecommendedStart"::text, t."RecommendedEnd"::text
        FROM lep."Tasks" t WHERE t."ProjectID" IN ({idlist})
          AND (t."RecommendedStart" IS NOT NULL OR t."RecommendedEnd" IS NOT NULL)''')
    tgt_rec = {r[0]: (r[1], r[2]) for r in q(tgt, f"""
        SELECT id::text, recommended_start_key, recommended_end_key
        FROM lep.workplan_items WHERE {mig}""")}
    n6 = Counter()
    for tid_src, rs, re_ in rep_rec:
        tid = det_id("task", tid_src.lower())
        if tid not in tgt_rec:
            n6["item_missing"] += 1
            continue
        ask, aek = tgt_rec[tid]
        for src_guid, actual in ((rs, ask), (re_, aek)):
            if not src_guid:
                continue
            exp = guid_to_key.get(src_guid.replace("-", "").upper())
            if exp is None:
                n6["unmapped_lookup"] += 1  # non-offset entry -> fallback path
            elif exp == actual:
                n6["mapped_match"] += 1
            else:
                n6["mapped_mismatch"] += 1
    print(f"  lookup-mapped: {dict(n6)}")
    results["sections"]["recommended_keys"] = {
        "coverage_start": cov_s, "coverage_end": cov_e, "total": len(keys),
        "bad_format": bad_fmt, "mapped": dict(n6)}

    # ------------- N7. injection countries — IBU country ids -------------
    print("\n=== N7. Injection countries resolved to IBU (2026-09-18 DB fix) ===")
    n7 = q(tgt, f"""
        SELECT bu.code, count(*)
        FROM lep.workplan_item_injection_countries ic
        JOIN lep.countries c ON c.id = ic.country_id
        JOIN lep.business_units bu ON bu.id = c.business_unit_id
        WHERE substring(ic.id::text, 15, 1) = '5'
        GROUP BY bu.code ORDER BY 2 DESC""")
    for code, cnt in n7:
        print(f"  {code}: {cnt:,}")
    obu = sum(c for code, c in n7 if code == "OBU")
    results["sections"]["injection_country_bu"] = {
        "by_bu": {code: c for code, c in n7}, "obu_rows": obu}

    rep.close()
    tgt.close()

    (OUT / "phase3.json").write_text(
        json.dumps(results, indent=2, default=_json_default), encoding="utf-8")
    print(f"\n[phase3] written {OUT / 'phase3.json'}")


if __name__ == "__main__":
    main()
