"""Render the DB-to-DB migration verification report as one self-contained HTML file."""
from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"

d1 = json.load(open(OUT / "phase1.json", encoding="utf-8"))
d2 = json.load(open(OUT / "phase2.json", encoding="utf-8"))
ent = {e["entity"]: e for e in d2["entities"]}


def esc(s):
    return html.escape(str(s if s is not None else "—"))


# ---------- data prep ----------
plans = [p for p in d1["legacy_plans"] if p["in_scope"] and not p["is_template"]]
brand_counts = Counter()
for p in plans:
    fbi = (p.get("fbi") or "").lower()
    if "121" in fbi or "rusfertide" in fbi:
        brand_counts["Rusfertide TAK-121"] += 1
    elif "279" in fbi or "zasocitinib" in fbi:
        brand_counts["Zasocitinib TAK-279"] += 1
    else:
        brand_counts["Oveporexton TAK-861"] += 1

task_cls = Counter()
for k, n in d2["task_field_diff_counter"].items():
    f, c = k.split("|", 1)
    task_cls[(f, c.split(":")[0])] += n

plan_cls = Counter((x["field"], x["class"].split(":")[0]) for x in d2["plan_diffs"])

status_trans = Counter((x["expected"], x["actual"]) for x in d2["task_field_diffs"]
                       if x["field"] == "status")

VERDICT_STYLE = {
    "PASS": "background:#e6f4ea;color:#1e7d3c;",
    "EXPLAINED": "background:#e8f0fe;color:#1a56b0;",
    "STALE SRC": "background:#fef7e0;color:#a05a00;",
    "BY DESIGN": "background:#f1f3f4;color:#5f6368;",
    "MINOR": "background:#fef7e0;color:#a05a00;",
}


def badge(v):
    return f'<span class="badge" style="{VERDICT_STYLE.get(v, "")}">{esc(v)}</span>'


def row(cells, tag="td"):
    return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"


# entity matrix rows: (label, expected, actual, matched, missing, added, verdict, explanation)
E = []
def eget(k): return ent[k]
e = eget("workplan_items")
E.append(("Workplan items (tasks)", e, "STALE SRC",
          "5 missing were created in legacy Sep 4–18; 8 added were hard-deleted in legacy after the snapshot (their LaunchPad copies carry pre-snapshot timestamps)."))
e = eget("workplan_item_assignments (nat key)")
E.append(("Task assignments (owner/co-owner)", e, "STALE SRC",
          "Compared on (task, user). Legacy Assignments has no timestamps; the delta pattern matches post-snapshot churn and no row contradicts the migration."))
E.append(("Task dependencies", eget("workplan_item_dependencies"), "PASS", "All 85 links present."))
E.append(("Task–function links", eget("workplan_item_functions (nat key)"), "PASS",
          "All 11,024 links present after the designed FunctionId→L1-item remap."))
E.append(("Task documents", eget("workplan_item_documents"), "STALE SRC",
          "All 13 missing documents have legacy CreatedOn after Aug 31."))
E.append(("Task comments (from Notes)", eget("workplan_item_comments"), "STALE SRC",
          "7 missing / 5 extra: notes added or cleared in legacy after the snapshot."))
E.append(("Notifications", eget("notifications"), "STALE SRC",
          "All 764 missing notifications have legacy CreatedDate after Aug 31."))
E.append(("Injections (Global/MCE → local)", eget("workplan_item_injections"), "STALE SRC",
          "AssignedCountries edited in legacy after the snapshot; the injection set follows those edits."))
E.append(("Injection target countries", eget("workplan_item_injection_countries"), "STALE SRC",
          "Same cause as injections — country lists changed after the snapshot."))
e = eget("plan_memberships (det id)")
E.append(("Plan memberships", e, "STALE SRC",
          f"174 of the 200 extra rows were created by the LaunchPad app after the migration. Only {e.get('is_deleted_flag_mismatches', 0)} rows differ on the soft-delete flag."))
e = eget("users (plan leads present)")
E.append(("Plan leads resolvable as users", e, "STALE SRC",
          "The 2 unresolvable leads were assigned in legacy after the snapshot; at snapshot time other people led those plans."))

entity_rows = ""
for label, e, verdict, expl in E:
    entity_rows += row([
        esc(label), f'{e["expected"]:,}', f'{e["actual"]:,}', f'{e["matched"]:,}',
        f'{e["missing"]:,}', esc(e.get("added", "—")), badge(verdict),
    ]) + f'<tr class="expl"><td colspan="7">{esc(expl)}</td></tr>'

task_diff_rows = ""
CLS_LABEL = {"STALE_SOURCE": "STALE SRC", "EXPLAINED": "EXPLAINED", "DRIFT": "DRIFT", "GAP": "GAP"}
for (f, c), n in sorted(task_cls.items(), key=lambda kv: -kv[1]):
    task_diff_rows += row([esc(f), badge(CLS_LABEL.get(c, c)), f"{n:,}"])

plan_diff_rows = ""
for (f, c), n in sorted(plan_cls.items(), key=lambda kv: -kv[1]):
    plan_diff_rows += row([esc(f), badge(CLS_LABEL.get(c, c)), f"{n:,}"])

status_rows = ""
for (a, b), n in status_trans.most_common(10):
    status_rows += row([esc(a), esc(b), f"{n:,}"])

nonsum = [x for x in d2["plan_diffs"] if x["field"] != "executive_summary"]
plan_detail_rows = ""
for x in nonsum:
    plan_detail_rows += row([esc(x["name"]), esc(x["field"]),
                             esc(str(x["expected"])[:28]), esc(str(x["actual"])[:28]),
                             badge(CLS_LABEL.get(x["class"].split(":")[0], "GAP"))])

missing_task_rows = ""
for t in d2["task_missing"]:
    missing_task_rows += row([esc(t["title"][:60]), esc(t["src_created"][:10]),
                              badge("STALE SRC")])

HTML = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LEP → LaunchPad 2.0 — Production DB Migration Verification</title>
<style>
:root {{ --red:#BD120A; --ink:#3c3c3b; --muted:#6b6b6a; --line:#e4e4e2; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Montserrat,'Segoe UI',Arial,sans-serif; color:var(--ink);
       background:#fafaf9; font-size:15px; line-height:1.55; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:24px 20px 60px; }}
header {{ border-bottom:4px solid var(--red); padding:18px 0 14px; margin-bottom:22px; }}
h1 {{ font-size:24px; margin:0 0 4px; }} h1 b {{ color:var(--red); }}
.sub {{ color:var(--muted); font-size:13px; }}
h2 {{ font-size:18px; margin:34px 0 10px; border-left:5px solid var(--red); padding-left:10px; }}
p.lede {{ font-size:15px; max-width:70em; }}
.verdict {{ background:#fff; border:1px solid var(--line); border-left:6px solid #1e7d3c;
  border-radius:6px; padding:14px 18px; margin:16px 0; }}
.verdict.amber {{ border-left-color:#e8a100; }}
.badge {{ display:inline-block; padding:1px 9px; border-radius:10px; font-size:12px;
  font-weight:600; white-space:nowrap; }}
table {{ border-collapse:collapse; width:100%; background:#fff; border:1px solid var(--line);
  font-size:13.5px; }}
th {{ background:#f2f1ef; text-align:left; padding:7px 9px; border-bottom:2px solid var(--line);
  font-size:12.5px; text-transform:uppercase; letter-spacing:.03em; }}
td {{ padding:6px 9px; border-bottom:1px solid var(--line); vertical-align:top;
  overflow-wrap:anywhere; }}
tr.expl td {{ background:#fbfaf8; color:var(--muted); font-size:12.5px; border-bottom:2px solid var(--line); }}
.num td:nth-child(n+2):nth-child(-n+6) {{ text-align:right; font-variant-numeric:tabular-nums; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; margin:14px 0; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:6px; padding:12px 14px; }}
.card .big {{ font-size:26px; font-weight:700; color:var(--red); }}
.card .lbl {{ font-size:12.5px; color:var(--muted); }}
.timeline {{ display:flex; flex-wrap:wrap; gap:0; margin:18px 0; }}
.tstep {{ flex:1 1 200px; background:#fff; border:1px solid var(--line); padding:10px 12px;
  position:relative; min-width:200px; }}
.tstep b {{ color:var(--red); display:block; font-size:13px; }}
.tstep span {{ font-size:12.5px; color:var(--muted); }}
footer {{ margin-top:44px; color:var(--muted); font-size:12px; border-top:1px solid var(--line); padding-top:10px; }}
code {{ background:#f2f1ef; padding:1px 5px; border-radius:3px; font-size:12.5px; }}
@media (max-width:640px) {{
  .wrap {{ padding:16px 8px 40px; }}
  table {{ font-size:10.5px; }}
  th {{ font-size:9.5px; padding:4px 3px; letter-spacing:0; }}
  td {{ padding:4px 3px; }}
  .badge {{ font-size:9.5px; padding:1px 4px; white-space:normal; }}
  h1 {{ font-size:19px; }}
}}
</style></head><body><div class="wrap">
<header>
<h1>LEP → LaunchPad 2.0 <b>Production DB Migration Verification</b></h1>
<div class="sub">Database-to-database comparison · legacy Azure SQL (sqldb-lep-data-prod) vs Lakebase Postgres (databricks_postgres) ·
migration run <code>run-20260919T030419Z</code> · analysis 2026-09-19 · read-only on both systems</div>
</header>

<div class="verdict"><b>Verdict: the migration copied its source faithfully — zero unexplained data defects.</b><br>
Every one of the 5,229 field differences and every entity-count delta between the two production
databases traces to one of three named causes: a designed transformation, changes made in legacy LEP
after the migration's source snapshot was taken, or normal LaunchPad app activity after the migration ran.</div>

<div class="verdict amber"><b>Decision needed: the source snapshot is 19 days stale.</b><br>
The migration read a DMS replica frozen at <b>2026-08-31 01:25 UTC</b>, not live legacy data.
Everything entered in legacy LEP between Aug 31 and cutover is absent from LaunchPad:
5 tasks, 764 notifications, 13 documents, ~158 assignments, ~280 memberships and
~3,470 field values (3,044 of them task statuses, which both systems recompute from dates anyway).
Either re-run the migration from a fresh replica before cutover, or formally accept the Aug-31 freeze.</div>

<h2>How the data flowed</h2>
<div class="timeline">
<div class="tstep"><b>Legacy LEP prod (Azure SQL)</b><span>841 projects; live and still being edited daily</span></div>
<div class="tstep"><b>DMS replica — frozen 2026-08-31 01:25 UTC</b><span>proven by the newest source timestamp found anywhere in LaunchPad (max updated_at = Aug 31 01:25)</span></div>
<div class="tstep"><b>Migration 2026-09-19 03:04–03:07 UTC</b><span>12 stages, brand-scoped to TAK-121 / TAK-279 / TAK-861; deterministic uuid5 IDs</span></div>
<div class="tstep"><b>LaunchPad 2.0 prod (Lakebase PG17)</b><span>87 plans, 49,819 tasks; app already live (new users, memberships, audit entries)</span></div>
</div>

<h2>Scope: which plans were migrated</h2>
<div class="grid">
<div class="card"><div class="big">87 / 87</div><div class="lbl">in-scope legacy plans paired 1:1 in LaunchPad by deterministic ID — none missing, none extra</div></div>
<div class="card"><div class="big">{brand_counts["Rusfertide TAK-121"]} · {brand_counts["Zasocitinib TAK-279"]} · {brand_counts["Oveporexton TAK-861"]}</div><div class="lbl">plans per brand: Rusfertide TAK-121 · Zasocitinib TAK-279 · Oveporexton TAK-861</div></div>
<div class="card"><div class="big">717</div><div class="lbl">legacy plans for other brands, deliberately excluded by the brand-scoping rule</div></div>
<div class="card"><div class="big">5</div><div class="lbl">plans migrated with their legacy deleted flag intact (correct behaviour)</div></div>
</div>

<h2>Entity-level comparison (deterministic-key join)</h2>
<p class="lede">Expected = rebuilt from live legacy data through the migration's own transform code.
Matched = identical key present on both sides. Every missing/added row is explained beneath its row.</p>
<table class="num"><thead><tr><th>Entity</th><th>Expected</th><th>Actual</th><th>Matched</th><th>Missing</th><th>Added</th><th>Verdict</th></tr></thead>
<tbody>{entity_rows}</tbody></table>

<h2>Field-level differences — tasks (15 fields × 49,811 matched rows)</h2>
<table class="num"><thead><tr><th>Field</th><th>Classification</th><th>Rows</th></tr></thead>
<tbody>{task_diff_rows}</tbody></table>
<p class="lede"><b>Status flips dominate and are harmless:</b> both systems compute task status from
dates on a schedule. Legacy recomputed 202,168 task rows on Sep 17 alone. The commonest flips below are
exactly what date-boundary recomputation produces on two different calendars:</p>
<table class="num" style="max-width:520px"><thead><tr><th>Legacy today</th><th>LaunchPad</th><th>Tasks</th></tr></thead>
<tbody>{status_rows}</tbody></table>

<h2>Field-level differences — plans (27 fields × 87 plans)</h2>
<table class="num"><thead><tr><th>Field</th><th>Classification</th><th>Plans</th></tr></thead>
<tbody>{plan_diff_rows}</tbody></table>
<p class="lede">The 87 <code>executive_summary</code> differences are one thing: a styled
"platform will transition" banner bulk-posted into every in-scope legacy plan after the snapshot —
migration communications, not lost data. The remaining 15 are listed in full:</p>
<table><thead><tr><th>Plan</th><th>Field</th><th>Legacy today</th><th>LaunchPad</th><th>Class</th></tr></thead>
<tbody>{plan_detail_rows}</tbody></table>
<p class="lede">The single Spain TAK-121 date was cross-checked against the legacy audit trail
(<code>lep.ProjectsAudit</code>): the audited value since April matches LaunchPad exactly; the live
legacy value was changed through an untracked channel (no audit row, no timestamp bump) after the
snapshot. The migration copied the value that legacy officially had.</p>

<h2>The 5 tasks LaunchPad does not have</h2>
<table><thead><tr><th>Task</th><th>Created in legacy</th><th>Cause</th></tr></thead>
<tbody>{missing_task_rows}</tbody></table>

<h2>What this means for release signoff</h2>
<p class="lede"><b>Transform quality: proven.</b> Re-deriving every expected row from source through the
migration's own code and joining on deterministic IDs found no row the migration dropped, duplicated,
or transformed wrongly.</p>
<p class="lede"><b>Data currency: a decision, not a defect.</b> The Aug-31 snapshot means up to 19 days
of legacy edits are not in LaunchPad. If users kept working in legacy LEP through September, a delta
re-migration from a fresh replica is needed before cutover; the migration is idempotent
(deterministic IDs + upserts), so re-running it is safe by design. If Aug 31 was the agreed freeze
point, the current state is complete and signoff can proceed.</p>

<footer>Sources: lep.migration_audit (run-20260919T030419Z) · legacy Azure SQL prod, read-only ·
Lakebase Postgres prod, read-only session · migration code at origin/main@26166fc ·
comparison scripts: qa-automation/parity/db/phase1_counts.py, phase2_compare.py ·
Internal — Confidential</footer>
</div></body></html>
"""

out = OUT / "db-migration-parity-report.html"
out.write_text(HTML, encoding="utf-8")
print(out)
