"""Shared batched upsert for the child-table loaders.

``load_item_children``, ``load_trackers`` and ``load_injections`` each write several
sibling tables from one pass over the source, in FK-safe order, with the same shape:
skip an empty batch, upsert on the deterministic uuid5 id, chunk the executemany,
and report a per-table count. That loop lives here once.
"""
from __future__ import annotations

from sqlalchemy import Engine, text

# (target table name, rows, column list) -- the column list drives both the INSERT
# and the DO UPDATE SET, with ``id`` excluded from the update.
Batch = tuple[str, list[dict], list[str]]


def upsert_batches(target: Engine, batches: list[Batch], *, label: str,
                   chunk: int = 5000) -> int:
    """Upsert each non-empty batch, print its count, return the total rows written."""
    total = 0
    with target.begin() as conn:
        for name, rows, cols in batches:
            if not rows:
                continue
            upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "id")
            up = text(f"INSERT INTO lep.{name} ({', '.join(cols)}) "
                      f"VALUES ({', '.join(':' + c for c in cols)}) "
                      f"ON CONFLICT (id) DO UPDATE SET {upd}")
            for i in range(0, len(rows), chunk):
                conn.execute(up, rows[i:i + chunk])
            total += len(rows)
            print(f"[{label}] {name}: {len(rows)}")
    return total


def report_dry_run(batches: list[Batch], *, label: str) -> int:
    """Print what each batch would write; return the total."""
    for name, rows, _ in batches:
        print(f"[{label}] DRY RUN {name}: {len(rows)}")
    return sum(len(rows) for _, rows, _ in batches)
