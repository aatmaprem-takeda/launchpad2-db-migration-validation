"""Reconcile legacy functions -> ``lep.functions`` (match by name, insert only new).

**Unblocked** — `functions` has no launch_plan FK, so this needs neither GEO-3/0.18
nor a loaded plan. Sources: the ``Function LT`` lookup category (18 values used by
``Tasks.Function``) + the ``lep.Functions`` table (1 row). Names normalized via the
verified §6.3 rule (`ComOps/ Cx` -> `ComOps/Cx`). Idempotent: ``ON CONFLICT (code)``.

Run standalone:  python -m scripts.migration.load_functions --dry-run
"""
from __future__ import annotations

import argparse
import re

from sqlalchemy import Engine, text

from scripts.migration.transforms import det_id, normalize_function

_SRC_LT = text(
    'SELECT le."Name" AS name FROM lep."LookupEntries" le '
    'JOIN lep."Lookups" lk ON lk."ID" = le."Lookup" '
    "WHERE lk.\"Name\" = 'Function LT' AND le.\"Active\" = TRUE"
)
_SRC_FN = text('SELECT "Name" AS name FROM lep."Functions"')

_UPSERT = text(
    "INSERT INTO lep.functions (id, code, name, is_active) "
    "VALUES (CAST(:id AS uuid), :code, :name, TRUE) "
    "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, is_active = EXCLUDED.is_active"
)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _legacy_names(source: Engine) -> set[str]:
    names: set[str] = set()
    with source.connect() as conn:
        for stmt in (_SRC_LT, _SRC_FN):
            for row in conn.execute(stmt):
                n = normalize_function(row.name)
                if n:
                    names.add(n)
    return names


def load(source: Engine, target: Engine, *, dry_run: bool = False) -> int:
    names = _legacy_names(source)

    # Reconcile by name against already-seeded functions; only insert the gaps.
    existing: set[str] = set()
    with target.connect() as conn:
        for row in conn.execute(text("SELECT name FROM lep.functions")):
            nn = normalize_function(row.name)
            if nn:
                existing.add(nn.lower())

    to_insert = []
    for n in sorted(names):
        if n.lower() in existing:
            continue  # already seeded -> reconciled, no-op
        code = _slug(n)
        to_insert.append({"id": det_id("function", code), "code": code, "name": n})

    if dry_run:
        print(f"[load_functions] {len(names)} legacy functions; "
              f"{len(to_insert)} new to insert, {len(names) - len(to_insert)} reconciled to seed")
        for r in to_insert:
            print(f"    + {r['code']:24} {r['name']}")
        return len(to_insert)

    with target.begin() as conn:
        for row in to_insert:
            conn.execute(_UPSERT, row)
    print(f"[load_functions] inserted {len(to_insert)} new functions "
          f"({len(names) - len(to_insert)} reconciled to existing seed)")
    return len(to_insert)


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load(source_engine(), target_engine(), dry_run=args.dry_run)
