"""Migration audit trail + source↔target reconciliation.

Every real ETL run is recorded to ``lep.migration_audit`` (created on first use)
and to a JSON report on disk — one row per stage (rows written, status, duration)
plus a row-count reconciliation (source vs target, with delta). This is the
migration's durable proof: what ran, when, how many rows, and whether counts
reconcile. Each row is committed independently, so the trail survives a mid-run
failure.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from sqlalchemy import Engine, text

AUDIT_DDL = text("""
CREATE TABLE IF NOT EXISTS lep.migration_audit (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        text        NOT NULL,
    seq           int         NOT NULL,
    kind          text        NOT NULL,   -- 'stage' | 'reconcile' | 'run'
    stage         text,
    target_table  text,
    rows_written  bigint,
    rows_source   bigint,
    rows_target   bigint,
    delta         bigint,
    status        text        NOT NULL,   -- 'ok' | 'error' | 'mismatch'
    message       text,
    duration_ms   bigint,
    recorded_at   timestamptz NOT NULL DEFAULT now()
)
""")

_COLS = ["run_id", "seq", "kind", "stage", "target_table", "rows_written",
         "rows_source", "rows_target", "delta", "status", "message", "duration_ms"]

# (label, source SQL Server count, target Postgres count) — reconciliation set.
RECON = [
    ("launch_plans", 'SELECT COUNT(*) FROM lep."Projects" WHERE COALESCE("IsTemplate", FALSE) = FALSE',
     "SELECT COUNT(*) FROM lep.launch_plans"),
    ("workplan_items", 'SELECT COUNT(*) FROM lep."Tasks"', "SELECT COUNT(*) FROM lep.workplan_items"),
    ("users", 'SELECT COUNT(DISTINCT LOWER("ResourceEmailAddress")) FROM lep."Resources"',
     "SELECT COUNT(*) FROM lep.users"),
    ("assignments", 'SELECT COUNT(*) FROM lep."Assignments"',
     "SELECT COUNT(*) FROM lep.workplan_item_assignments"),
    ("dependencies", 'SELECT COUNT(*) FROM lep."Dependencies"',
     "SELECT COUNT(*) FROM lep.workplan_item_dependencies"),
    ("memberships", 'SELECT COUNT(*) FROM lep."ProjectResources"',
     "SELECT COUNT(*) FROM lep.plan_memberships"),
    ("trackers", 'SELECT COUNT(*) FROM lep."Tracker"', "SELECT COUNT(*) FROM lep.trackers"),
    ("notifications", 'SELECT COUNT(*) FROM lep."Notifications"', "SELECT COUNT(*) FROM lep.notifications"),
]


class MigrationAudit:
    def __init__(self, target: Engine, run_id: str | None = None):
        self.target = target
        self.run_id = run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
        self.seq = 0
        self.rows: list[dict] = []
        with target.begin() as c:
            c.execute(AUDIT_DDL)

    def _insert(self, **kw):
        self.seq += 1
        rec = {c: None for c in _COLS}
        rec.update(run_id=self.run_id, seq=self.seq, **kw)
        self.rows.append(rec)
        with self.target.begin() as c:
            c.execute(text(f"INSERT INTO lep.migration_audit ({', '.join(_COLS)}) "
                           f"VALUES ({', '.join(':' + x for x in _COLS)})"),
                      {k: rec.get(k) for k in _COLS})

    def stage(self, name, target_table, rows_written, status="ok", message=None, duration_ms=None):
        self._insert(kind="stage", stage=name, target_table=target_table,
                     rows_written=rows_written, status=status, message=message, duration_ms=duration_ms)

    def run(self, status, message=None, duration_ms=None):
        self._insert(kind="run", stage="run_all", status=status, message=message, duration_ms=duration_ms)

    def reconcile(self, source: Engine):
        """Compare source vs target row counts; record each (delta != 0 -> 'mismatch')."""
        for label, ssql, tsql in RECON:
            try:
                with source.connect() as c:
                    s = c.execute(text(ssql)).scalar()
                with self.target.connect() as c:
                    t = c.execute(text(tsql)).scalar()
                delta = (t or 0) - (s or 0)
                self._insert(kind="reconcile", stage="reconcile", target_table=label,
                             rows_source=s, rows_target=t, delta=delta,
                             status="ok" if delta == 0 else "mismatch")
            except Exception as e:  # noqa: BLE001 - record and continue
                self._insert(kind="reconcile", stage="reconcile", target_table=label,
                             status="error", message=str(e)[:300])

    def write_report(self, path: str | None = None) -> str:
        p = path or f"migration_audit_{self.run_id}.json"
        with open(p, "w") as fh:
            json.dump({"run_id": self.run_id, "rows": self.rows}, fh, indent=2, default=str)
        return p


def timed(fn):
    """Run fn(), return (result, duration_ms, error) without raising."""
    t0 = time.time()
    try:
        return fn(), int((time.time() - t0) * 1000), None
    except Exception as e:  # noqa: BLE001
        return None, int((time.time() - t0) * 1000), e
