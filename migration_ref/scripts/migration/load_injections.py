"""T29 — reconstruct task injections into ``workplan_item_injections`` (+ countries).

The Global/MCE -> Local relationship is task-level (0.18): a task in a
Global or MCE plan, with target countries assigned, is "injected" into the matching
descendant plans. Here we mark those source tasks and record their target countries
(injecting into the MCE plan == the synthetic "MCE" country).

Derivation:
  source Task where  plan_type in (global, mce)
                     AND  (IsMilestone=1 OR IsMCEActivity=1)
                     AND  AssignedCountries set
  -> workplan_item_injections(workplan_item_id = that task)
  -> workplan_item_injection_countries(one per assigned country; 'MCE' -> MCE pseudo-country)

Runs after load_workplan_items. Idempotent uuid5 + ON CONFLICT (id).
"""
from __future__ import annotations

import argparse
import re

from sqlalchemy import Engine, text

from scripts.migration.transforms import canonical_country, det_id
from scripts.migration.writer import upsert_batches

_SPLIT = re.compile(r"[;,|]")


_LABEL = "load_injections"

_SRC = text('SELECT t."ID", t."ProjectID", t."AssignedCountries" FROM lep."Tasks" t '
            'WHERE (t."IsMilestone" = TRUE OR t."IsMCEActivity" = TRUE) '
            'AND t."AssignedCountries" IS NOT NULL')


def _target_lookups(target: Engine, *, dry_run: bool = False, _dry_state: dict | None = None):
    with target.connect() as c:
        if dry_run and _dry_state and "plan_types" in _dry_state:
            plan_type = _dry_state["plan_types"]
        else:
            plan_type = {str(r.id): r.plan_type for r in c.execute(
                text("SELECT id, plan_type FROM lep.launch_plans"))}
        if dry_run and _dry_state and "item_ids" in _dry_state:
            items = _dry_state["item_ids"]
        else:
            items = {str(r.id) for r in c.execute(text("SELECT id FROM lep.workplan_items"))}
        # lep.countries has multiple rows per country name (one per business unit).
        # Resolve in two passes: first all countries, then IBU-specific overrides.
        # All migrated templates are IBU-scoped, so injection countries must use
        # IBU country_ids — not OBU/USBU variants of the same market.
        countries = {r.name.strip().lower(): str(r.id)
                     for r in c.execute(text("SELECT name, id FROM lep.countries"))}
        # Overlay: prefer IBU country_ids for multi-BU markets (e.g. Spain
        # exists as both IBU and OBU — always pick IBU for injections).
        for r in c.execute(text(
                "SELECT c.name, c.id "
                "FROM lep.countries c "
                "JOIN lep.business_units bu ON bu.id = c.business_unit_id "
                "WHERE bu.name = 'IBU'")):
            countries[r.name.strip().lower()] = str(r.id)
    return plan_type, items, countries


def _target_countries(assigned, countries) -> list[str]:
    """The delimited AssignedCountries fan-out (T21), resolved to country ids."""
    targets = []
    for raw in _SPLIT.split(assigned):
        cid = countries.get((canonical_country(raw) or "").lower())
        if cid:
            targets.append(cid)
    return targets


def _read_injections(conn, plan_type, items, countries):
    """A milestone or MCE-activity task in a Global/MCE plan with target countries
    assigned is an injection source. -> ``(injections, injection_countries)``."""
    inj, inj_countries = [], []
    for r in conn.execute(_SRC):
        plan_id = det_id("plan", str(r.ProjectID))
        wi = det_id("task", str(r.ID))
        if plan_type.get(plan_id) not in ("global", "mce") or wi not in items:
            continue
        targets = _target_countries(r.AssignedCountries, countries)
        if not targets:
            continue
        inj_id = det_id("injection", wi)
        inj.append({"id": inj_id, "workplan_item_id": wi, "is_deleted": False})
        for cid in set(targets):
            inj_countries.append({"id": det_id("injcountry", inj_id, cid),
                                  "workplan_item_injection_id": inj_id,
                                  "country_id": cid, "is_deleted": False})
    return inj, inj_countries


def _load_impl(source: Engine, target: Engine, maps=None, *, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    plan_type, items, countries = _target_lookups(target, dry_run=dry_run, _dry_state=_dry_state)

    with source.connect() as conn:
        inj, inj_countries = _read_injections(conn, plan_type, items, countries)

    if dry_run:
        print(f"[{_LABEL}] DRY RUN: {len(inj)} injections, "
              f"{len(inj_countries)} target countries")
        return len(inj)

    batches = [
        ("workplan_item_injections", inj, ["id", "workplan_item_id", "is_deleted"]),
        ("workplan_item_injection_countries", inj_countries,
         ["id", "workplan_item_injection_id", "country_id", "is_deleted"]),
    ]
    total = upsert_batches(target, batches, label=_LABEL)
    print(f"[{_LABEL}] upserted {total} rows")

    # Post-upsert: injected tasks must be flagged is_milestone=true so the
    # app renders them as milestones in local plan views.  Source MCE tasks
    # often have IsMilestone=false despite functioning as injected milestones.
    if inj:
        item_ids = [i["workplan_item_id"] for i in inj]
        with target.begin() as conn:
            ms_updated = 0
            for wid in item_ids:
                r = conn.execute(text(
                    "UPDATE lep.workplan_items SET is_milestone = true "
                    "WHERE id = CAST(:id AS uuid) AND is_milestone = false "
                    "RETURNING id"
                ), {"id": wid}).fetchone()
                if r:
                    ms_updated += 1
            if ms_updated:
                print(f"[{_LABEL}] marked {ms_updated} injected items as milestones")

    return total


def load(source: Engine, target: Engine, maps=None, *, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    return _load_impl(source, target, maps, dry_run=dry_run, _dry_state=_dry_state)


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    load(source_engine(), target_engine(), dry_run=a.dry_run)
