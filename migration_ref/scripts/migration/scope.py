"""Migration scope — which launches the migration loads.

The migration scope IS the set of brands below: the migration loads **only** the
plans whose FranchiseBrandIndication matches one of these brand/indication combos.
This is the whole migration, not a pilot — the rest of the legacy plans are out of
scope by decision.

Override for a full-DB run (every non-template plan) with the env var:

    LEP_MIGRATION_SCOPE=full  python -m scripts.migration.run_all --dry-run

Why this is the only file that needs changing: every downstream loader gates on
the plans that ``load_plans`` actually wrote (``SELECT id FROM lep.launch_plans``)
and on ``workplan_items``. So filtering the *plans* here automatically cascades to
tasks, memberships, trackers, notifications and injections — no other loader
changes are required.

Matching is case-insensitive substring against the resolved FBI string
('TA.Brand.Indication'), on brand name **or** molecule code. Confirm/adjust the
tokens with the scope discovery query in ``docs/migration/verify-legacy.sql``.
"""
from __future__ import annotations

import os

# The migration scope: label -> match tokens (brand name + molecule-code variants).
# A plan is in scope if its FBI contains ANY token of ANY brand. Add/adjust tokens
# once the discovery query confirms how each brand is named in lep.LookupEntries
# (dev names by molecule code, e.g. '...TAK-861.NT1').
MIGRATION_BRANDS: list[dict] = [
    {"label": "Rusfertide (TAK-121) — Polycythemia Vera",
     "tokens": ("rusfertide", "tak-121", "tak121")},
    {"label": "Oveporexton (TAK-861) — Narcolepsy Type 1",
     "tokens": ("oveporexton", "tak-861", "tak861")},
    {"label": "Zasocitinib (TAK-279) — Plaque Psoriasis",
     "tokens": ("zasocitinib", "tak-279", "tak279")},
]


def _active_brands() -> list[dict] | None:
    """The brand scope (default), or None for an explicit full-DB migration.

    Default = scoped to MIGRATION_BRANDS. Set LEP_MIGRATION_SCOPE=full (or 'all')
    to migrate every non-template plan instead.
    """
    mode = os.environ.get("LEP_MIGRATION_SCOPE", "brands").strip().lower()
    if mode in ("full", "all"):
        return None
    return MIGRATION_BRANDS  # default: scoped to the migration brands


def is_full_migration() -> bool:
    """True only when LEP_MIGRATION_SCOPE=full — every non-template plan."""
    return _active_brands() is None


def scope_label() -> str:
    brands = _active_brands()
    if brands is None:
        return "FULL (all non-template plans — LEP_MIGRATION_SCOPE=full)"
    return f"BRANDS ({len(brands)}: " + "; ".join(b["label"] for b in brands) + ")"


def in_scope(fbi_name: str | None) -> bool:
    """True if this plan's FranchiseBrandIndication is in the active scope."""
    brands = _active_brands()
    if brands is None:
        return True                       # full migration — everything is in scope
    if not fbi_name:
        return False                      # brand-scoped run: no FBI -> out of scope
    hay = fbi_name.lower()
    return any(tok in hay for brand in brands for tok in brand["tokens"])
