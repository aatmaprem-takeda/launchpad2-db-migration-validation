"""Deterministic IDs + the verified value crosswalks.

Every crosswalk here was verified against the live legacy DB on 2026-07-30
(guide §6.3). Transforms are pure functions so they can be unit-tested without a
database — run ``python -m scripts.migration.transforms`` for a self-check.
"""
from __future__ import annotations

import uuid

# Reuse the seed namespace so migration IDs reconcile with seed_from_dump.py.
NS = uuid.UUID("6b1d1f2e-0000-4c00-8000-1e9000000000")


def det_id(*parts: str) -> str:
    """Deterministic uuid5 from lowercased, stripped parts (idempotent re-runs)."""
    return str(uuid.uuid5(NS, "|".join(p.strip().lower() for p in parts)))


def user_id_for_email(email: str) -> str:
    """Stable user id keyed by email — matches seed_from_dump's ``det_id('user', email)``."""
    return det_id("user", email.strip().lower())


def _norm(v: object) -> str:
    return str(v).strip().lower() if v is not None else ""


# --- Plan type (T10, §3.5) — bit flags are the sole discriminator ----------
def classify_plan_type(is_template, is_global, is_mce) -> str:
    """Return 'template' | 'mce' | 'global' | 'local'. IsTemplate wins, then MCE, then Global."""
    if bool(is_template):
        return "template"          # -> launch_templates, NOT launch_plans (0.8)
    if bool(is_mce):
        return "mce"
    if bool(is_global):
        return "global"
    return "local"


# --- Global-plan BU assignment (GEO-3, RESOLVED 2026-08-03 via Confluence/Rovo) --
# BF->BU grid: SPD->Global, IBU->MCE/Local, USBU->Local, OBU->Global/Local.
# OBU handles Oncology only; SPD handles all TAs. So a Global plan's BU is OBU if
# its therapeutic area is Oncology, else SPD. IBU is NOT valid for Global (the
# documented "IBU sometimes runs Global" exception is not enforced -> case-by-case).
def global_plan_bu(therapeutic_area: str | None) -> str:
    """BU code for a Global plan from its TA: Oncology -> OBU, else -> SPD."""
    return "OBU" if _norm(therapeutic_area) == "oncology" else "SPD"


# OBU's covered markets (from the authoritative Market-Country mapping, 2026-08-03).
# OBU has NO separate geo-taxonomy — it uses IBU's countries; only the plan's BU
# differs. So an Oncology LOCAL plan in one of these countries is owned by OBU.
# (Gated by 0.19: if OBU is out of the initial LaunchPad 2.0 scope, disable this.)
OBU_COVERAGE = {"canada", "france", "germany", "ireland", "italy", "japan",
                "portugal", "spain", "united kingdom", "united states"}


def local_plan_bu(therapeutic_area: str | None, country_name: str | None,
                  country_default_bu_code: str | None) -> str | None:
    """Local plan BU: OBU if Oncology in an OBU-covered country, else the country's own BU."""
    if _norm(therapeutic_area) == "oncology" and _norm(country_name) in OBU_COVERAGE:
        return "OBU"
    return country_default_bu_code


# --- Plan health: OverallLaunchStatus -> launch_plans.health_status (§6.3-A) -
# CHECK: healthy | stable | at_risk | critical | NULL
_HEALTH = {"green": "healthy", "amber": "at_risk", "red": "critical", "completed": None}


def plan_health_status(overall_launch_status_label: str | None):
    """Map the RAG label to health_status; 'Completed' -> None (set status=completed instead)."""
    return _HEALTH.get(_norm(overall_launch_status_label))


def plan_status_from_overall(overall_launch_status_label: str | None) -> str | None:
    """'Completed' overall status implies plan status 'completed'; else leave to caller."""
    return "completed" if _norm(overall_launch_status_label) == "completed" else None


# --- Task workflow: Tasks.LepStatus -> workplan_items.status (§6.3-B) --------
# CHECK: not_started | in_progress | delayed | completed | blocked | cancelled
_TASK_STATUS = {
    "not started": "not_started",
    "in progress": "in_progress",
    "delayed": "delayed",
    "completed": "completed",
    "green": "not_started",   # 14 anomalous RAG-in-workflow rows (§6.3-B) -> not_started
}


def task_status(lep_status_label: str | None) -> str:
    """Map legacy LepStatus to the workplan_items.status vocab. Unknown -> not_started."""
    return _TASK_STATUS.get(_norm(lep_status_label), "not_started")


# --- Task RAG: Tasks.Status is NOT workflow -> is_at_risk (§6.3-C) ----------
def task_is_at_risk(status_rag_label: str | None) -> bool:
    """Amber/Red -> at risk; Green/Complete/Not Applicable/None -> not."""
    return _norm(status_rag_label) in {"amber", "red"}


# --- Country alias (GEO-4, §3.4) — verified: only these two are needed ------
COUNTRY_ALIAS = {
    "greater ecuador": "Ecuador",
    "carribean": "Caribbean",
    # defensive (present in dbo mirror, not in lep plans, but cheap to keep):
    "greater china": "China",
    "greator ecuador": "Ecuador",
    "macedonia": "North Macedonia",
    "bosnia-herzegovina": "Bosnia and Herzegovina",
    "turkey": "Türkiye",        # ISO 3166 official name change 2022-06
    # "tet" — garbage token in source; no real country, silently dropped
}

# Countries in legacy plans with NO row in the new taxonomy (GEO-4 decision).
BLOCKED_COUNTRIES = {"hong kong"}  # 13 live plans; add to taxonomy or map (business)


def canonical_country(country_name: str | None) -> str | None:
    """Return the taxonomy-canonical country name, applying the alias map."""
    if country_name is None:
        return None
    return COUNTRY_ALIAS.get(_norm(country_name), country_name.strip())


def split_region_country(entry_name: str | None):
    """`Takeda Region Country LT` entries are 'Region.Country' -> (region, country)."""
    if not entry_name:
        return (None, None)
    region, _, country = entry_name.partition(".")
    return (region.strip() or None, (country.strip() or None) if country else None)


# --- Function name normalization (§6.3-D) -----------------------------------
def normalize_function(name: str | None) -> str | None:
    """Normalize the slashed labels ('ComOps/ Cx' -> 'ComOps/Cx') for reconcile-by-name."""
    if name is None:
        return None
    return " ".join(name.replace("/ ", "/").split())


# --- Membership role (§6.3-E, 0.14) -----------------------------------------
# ProjectResources.Role is a FUNCTION, not an RBAC role. All such members map to
# 'team_member'; the function is preserved separately (see load_memberships TODO).
MEMBERSHIP_ROLE_CODE = "team_member"
OWNER_ROLE_CODE = "launch_lead"
SECONDARY_LEAD_ROLE_CODE = "secondary_launch_lead"


# --- Dependency type (T25) --------------------------------------------------
# lep.Dependencies.Type is an int code (confirm the code table against target vocab).
_DEP_TYPE = {0: "FS", 1: "SS", 2: "FF", 3: "SF"}


def dependency_type(code) -> str:
    try:
        return _DEP_TYPE.get(int(code), "FS")
    except (TypeError, ValueError):
        return "FS"


if __name__ == "__main__":  # lightweight self-check (no DB)
    assert classify_plan_type(1, 1, 1) == "template"
    assert classify_plan_type(0, 1, 0) == "global"
    assert classify_plan_type(0, 0, 1) == "mce"
    assert classify_plan_type(0, 0, 0) == "local"
    assert plan_health_status("Green") == "healthy"
    assert plan_health_status("Red") == "critical"
    assert plan_health_status("Completed") is None
    assert plan_status_from_overall("Completed") == "completed"
    assert task_status("Delayed") == "delayed"
    assert task_status("Not Started") == "not_started"
    assert task_status("Green") == "not_started"
    assert task_is_at_risk("Amber") and not task_is_at_risk("Green")
    assert canonical_country("Greater Ecuador") == "Ecuador"
    assert canonical_country("Carribean") == "Caribbean"
    assert global_plan_bu("Oncology") == "OBU"
    assert global_plan_bu("Rare Hematology") == "SPD"
    assert global_plan_bu("Vaccines") == "SPD"
    assert local_plan_bu("Oncology", "Germany", "IBU") == "OBU"        # OBU-covered
    assert local_plan_bu("Oncology", "Brazil", "IBU") == "IBU"         # not OBU-covered
    assert local_plan_bu("Vaccines", "Germany", "IBU") == "IBU"        # not oncology
    assert canonical_country("France") == "France"
    assert split_region_country("GEM - CHINA.Hong Kong") == ("GEM - CHINA", "Hong Kong")
    assert split_region_country("Global.Global") == ("Global", "Global")
    assert normalize_function("ComOps/ Cx") == "ComOps/Cx"
    assert user_id_for_email("A@B.com ") == user_id_for_email("a@b.com")
    print("transforms self-check OK")
