"""Load ``lep.Tracker`` -> ``trackers`` and ``lep.TrackerAssociations`` ->
``tracker_associations``.

NOTE: the raw ``Tracker`` rows are written to the legacy DB by an EXTERNAL feed
(Interact/SPOT/PriceRight) — this migrates the historical data; the live feed must
be re-routed at cutover (see External Systems). Association tracker refs are
resolved via (Source, SourcePrimaryKey) — confirm the join if counts look off
(only ~9 association rows). Idempotent uuid5 + ON CONFLICT (id).
"""
from __future__ import annotations

import argparse

from sqlalchemy import Engine, text

from scripts.migration.transforms import det_id
from scripts.migration.writer import report_dry_run, upsert_batches

_LABEL = "load_trackers"

# association column -> the legacy feed that column points at
_FEEDS = (("INTERACT", "InteractProjectID"), ("SPOT", "SpotProjectID"),
          ("MANUAL", "ManualProjectID"), ("PRICERIGHT", "PriceRightProjectID"))

_ASSOC_SQL = text(
    'SELECT "ID", "LEPProjectID", "InteractProjectID", "SpotProjectID", "ManualProjectID", '
    '"PriceRightProjectID", "LaunchDateSource", "RegulatoryApprovalSource", '
    '"PricingApprovalSource", "ReimbursementApprovalSource", "Status" '
    'FROM lep."TrackerAssociations"')

_TRACKER_SQL = text(
    'SELECT "ID", "Source", "SourcePrimaryKey", "SourceProduct", "LaunchDate", '
    '"RegulatoryApprovalDate", "PricingApprovalDate", "ReimbursementApprovalDate" '
    'FROM lep."Tracker"')


def _read_in_scope_associations(conn, plans):
    """In-scope associations FIRST, collecting the tracker source-keys they reference
    -- so we migrate ONLY the trackers linked to in-scope plans (scope: trackers for
    the migrated brands alone, not the whole feed)."""
    raw_assoc, referenced = [], set()
    for r in conn.execute(_ASSOC_SQL):
        plan_id = det_id("plan", str(r.LEPProjectID))
        if plan_id not in plans:
            continue
        raw_assoc.append((r, plan_id))
        for src_name, attr in _FEEDS:
            pid = getattr(r, attr)
            if pid is not None:
                referenced.add((src_name, str(pid)))
    return raw_assoc, referenced


def _read_referenced_trackers(conn, referenced):
    """Load ONLY the trackers referenced by the in-scope associations."""
    trackers, by_source_key = [], {}
    for r in conn.execute(_TRACKER_SQL):
        key = ((str(r.Source).upper(), str(r.SourcePrimaryKey))
               if r.Source and r.SourcePrimaryKey is not None else None)
        if key is None or key not in referenced:
            continue   # not linked to an in-scope plan -> out of scope
        tid = det_id("tracker", str(r.ID))
        trackers.append({"id": tid, "project_name": "(tracker)",   # source has no ProjectName
                         # `source` is NOT NULL with no DB default -- carry the
                         # legacy feed name (INTERACT/SPOT/MANUAL/PRICERIGHT)
                         "source": key[0],
                         "source_primary_key": r.SourcePrimaryKey,
                         "source_product": r.SourceProduct,
                         "launch_date": r.LaunchDate,
                         "regulatory_approval_date": r.RegulatoryApprovalDate,
                         "pricing_approval_date": r.PricingApprovalDate,
                         "reimbursement_approval_date": r.ReimbursementApprovalDate})
        by_source_key[key] = tid
    return trackers, by_source_key


def _build_associations(raw_assoc, by_source_key):
    """Resolve each association's (now-scoped) tracker refs."""
    def ref(source_name, pid):
        return by_source_key.get((source_name, str(pid))) if pid is not None else None

    assoc = []
    for r, plan_id in raw_assoc:
        assoc.append({"id": det_id("trackerassoc", str(r.ID)), "launch_plan_id": plan_id,
                      "interact_tracker_id": ref("INTERACT", r.InteractProjectID),
                      "spot_tracker_id": ref("SPOT", r.SpotProjectID),
                      "manual_tracker_id": ref("MANUAL", r.ManualProjectID),
                      "price_right_tracker_id": ref("PRICERIGHT", r.PriceRightProjectID),
                      "launch_date_source": r.LaunchDateSource or "manual",
                      "regulatory_approval_date_source": r.RegulatoryApprovalSource,
                      "pricing_approval_date_source": r.PricingApprovalSource,
                      "reimbursement_approval_date_source": r.ReimbursementApprovalSource,
                      "status": r.Status})
    return assoc


def load(source: Engine, target: Engine, maps=None, *, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    if dry_run and _dry_state and "plan_ids" in _dry_state:
        plans = _dry_state["plan_ids"]
    else:
        with target.connect() as c:
            plans = {str(r.id) for r in c.execute(text("SELECT id FROM lep.launch_plans"))}

    with source.connect() as conn:
        raw_assoc, referenced = _read_in_scope_associations(conn, plans)
        trackers, by_source_key = _read_referenced_trackers(conn, referenced)
        assoc = _build_associations(raw_assoc, by_source_key)

    batches = [
        ("trackers", trackers,
         ["id", "project_name", "source", "source_primary_key", "source_product",
          "launch_date", "regulatory_approval_date", "pricing_approval_date",
          "reimbursement_approval_date"]),
        ("tracker_associations", assoc,
         ["id", "launch_plan_id", "interact_tracker_id", "spot_tracker_id",
          "manual_tracker_id", "price_right_tracker_id", "launch_date_source",
          "regulatory_approval_date_source", "pricing_approval_date_source",
          "reimbursement_approval_date_source", "status"]),
    ]
    if dry_run:
        return report_dry_run(batches, label=_LABEL)

    total = upsert_batches(target, batches, label=_LABEL, chunk=2000)
    print(f"[{_LABEL}] upserted {total} rows")
    return total


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load(source_engine(), target_engine(), dry_run=a.dry_run)
