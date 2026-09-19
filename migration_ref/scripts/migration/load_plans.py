"""Load ``lep.Projects`` -> ``lep.launch_plans``.

Both blockers resolved (2026-08-03): **GEO-3** (Global-plan BU via therapeutic
area — ``transforms.global_plan_bu``) and **0.18** (family is implicit by
``brand_molecule_id``; ``parent_plan_id`` stays NULL — verified V20). So this
loader is unblocked.

Reads the source (SQL Server) via SQLAlchemy + the resolution maps (LookupEntries
/ Resources), resolves the target FKs from the already-seeded PostgreSQL tables,
applies the verified §6.3 crosswalks, and upserts. Idempotent: ``id =
uuid5('plan', legacy ID)``, ``ON CONFLICT (id)``.

Excludes ``IsTemplate=1`` rows (they go to launch_templates, 0.8). Carries
``IsDeleted`` as ``is_deleted`` (T14).
"""
from __future__ import annotations

import argparse

from sqlalchemy import Engine, text

from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.scope import in_scope, scope_label
from scripts.migration.transforms import (
    canonical_country, classify_plan_type, det_id, global_plan_bu, local_plan_bu,
    plan_health_status, plan_status_from_overall, split_region_country,
    user_id_for_email,
)

# --- source date column -> target column (T1 datetime->date) -----------------
DATE_MAP = {
    "RegulatorySubmissionDate": "regulatory_submission_date",
    "RegulatoryApprovalDate": "regulatory_approval_date",
    "CommercialLaunchDate": "commercial_launch_date",
    "PricingSubmissionDate": "pricing_submission_date",
    "PricingApprovalDate": "pricing_approval_date",
    "ReimbursementSubmissionDate": "reimbursement_submission_date",
    "ReimbursementApprovalDate": "reimbursement_approval_date",
    "TradeStockAvailableDate": "trade_stock_available_date",
    "Phase3resultsDate": "phase_3_results_date",
    "LaunchManagementCountryCheckpointReviewL18M": "checkpoint_l_minus_18m_date",
    "LaunchManagementCountryCheckpointReviewL12M": "checkpoint_l_minus_12m_date",
    "LaunchManagementCountryGoNoGoGateL6M": "gate_l_minus_6m_date",
    "LaunchManagementCountryCheckpointReviewL6M": "checkpoint_l_plus_6m_date",
    "GPSCheckPointReview1Date": "gps_checkpoint_1_date",
    "GPSCheckPointReview2Date": "gps_checkpoint_2_date",
    "GPSCheckPointReview3Date": "gps_checkpoint_3_date",
    "GPSCheckPointReview4Date": "gps_checkpoint_4_date",
    "GPSCheckPointReview5Date": "gps_checkpoint_5_date",
    "GPSGoNoGoGateDate": "gps_go_no_go_date",
}

_SRC = text('SELECT * FROM lep."Projects"')

# FBI indication names that differ from the seeded target names.
_INDICATION_ALIASES: dict[str, str] = {
    "pv": "polycythemia vera",
    "psoriasis": "plaque psoriasis (pso)",
    "psoriatic arthritis": "psoriatic arthritis (psa)",
}


def _split_fbi(fbi_name: str | None) -> tuple[str | None, str | None, str | None]:
    """'TA.Brand.Indication' -> (TA, Brand, Indication). Indication is None when the
    entry carries only two segments."""
    if not fbi_name or "." not in fbi_name:
        return None, None, None
    ta, rest = fbi_name.split(".", 1)
    if "." in rest:
        brand, indication = rest.rsplit(".", 1)
    else:
        brand, indication = rest, None
    return (ta.strip() or None,
            brand.strip() or None,
            (indication or "").strip() or None)


def _brand_molecule_id(fbi_name: str | None) -> str | None:
    """FranchiseBrandIndication 'TA.Brand.Indication' -> uuid5('brand', TA, Brand).
    Matches seed_from_dump's det_id('brand', Franchise, Brand)."""
    ta, brand, _ = _split_fbi(fbi_name)
    if not (ta and brand):
        return None
    return det_id("brand", ta, brand)


def _target_maps(target: Engine):
    """Lookup dicts from the seeded PostgreSQL.

    Returns ``(bu_by_code, bu_code_by_id, countries_by_name, indications, brands_by_name)``.

    ``countries_by_name`` maps a lowercased country name to the LIST of rows that
    carry it -- ``lep.countries`` is unique on ``(business_unit_id, iso_code)``, not
    on the name, so a market managed by several BUs holds one row per BU (Germany,
    Japan, ... under IBU *and* OBU; 'Global' under IBU, SPD *and* OBU). Collapsing
    those to a single entry silently picked an arbitrary BU's row, which then
    contradicted the plan's own ``business_unit_id`` -- so keep every candidate and
    let ``_resolve_geography`` pick the one whose BU matches the plan.

    ``indications`` maps ``(brand_molecule_id, lowercased indication name)`` ->
    indication id, for the plan's ``indication_id`` (part of the launch uniqueness
    key: brand/molecule + country + indication).
    """
    bu, id_to_code, countries, indications = {}, {}, {}, {}
    brands_by_name = {}
    valid_bm_ids: set[str] = set()
    with target.connect() as c:
        for r in c.execute(text("SELECT code, id FROM lep.business_units")):
            bu[r.code] = str(r.id); id_to_code[str(r.id)] = r.code
        for r in c.execute(text("SELECT name, id, business_unit_id FROM lep.countries")):
            bu_id = str(r.business_unit_id)
            countries.setdefault(r.name.strip().lower(), []).append(
                (str(r.id), bu_id, id_to_code.get(bu_id)))
        for r in c.execute(text("SELECT id, brand_molecule_id, name FROM lep.indications")):
            indications[(str(r.brand_molecule_id), r.name.strip().lower())] = str(r.id)
        for r in c.execute(text("SELECT id, brand_name FROM lep.brand_molecules")):
            name = r.brand_name.strip().lower()
            brands_by_name[name] = str(r.id)
            valid_bm_ids.add(str(r.id))
            # Prefix alias: "tak-121 rusfertide" also indexed as "tak-121"
            first_token = name.split()[0] if " " in name else None
            if first_token and first_token not in brands_by_name:
                brands_by_name[first_token] = str(r.id)
    return bu, id_to_code, countries, indications, brands_by_name, valid_bm_ids


def _resolve_geography(ptype, ta, country, candidates, bu_by_code):
    """Pick ``(country_id, business_unit_id)`` for a plan.

    BU is decided FIRST (GEO-3/GEO-5), then the country row is chosen from the
    same-name candidates so the plan's BU and its country row's BU agree. OBU has
    no geo taxonomy of its own -- it reuses IBU's markets -- so the "country's own
    BU" for the local rule is the first non-OBU candidate.
    """
    if not candidates:
        return None, None

    if ptype == "global":
        bu_code = global_plan_bu(ta)                       # Onc -> OBU, else SPD
    elif ptype == "mce":
        bu_code = "IBU"
    else:
        own = next((code for _, _, code in candidates if code != "OBU"),
                   candidates[0][2])
        bu_code = local_plan_bu(ta, country, own)

    bu_id = bu_by_code.get(bu_code)
    # the country row belonging to the chosen BU; fall back to the first candidate
    # (a name with only one row always hits this on the first branch anyway)
    country_id = next((cid for cid, cbu, _ in candidates if cbu == bu_id), None)
    if country_id is None:
        country_id = candidates[0][0]
    return country_id, bu_id


def _day_of_month(v):
    try:
        return v.day
    except AttributeError:
        return None


# outcomes of classifying one legacy Projects row
_TEMPLATE, _OUT_OF_SCOPE, _UNRESOLVED, _OK = "template", "out_of_scope", "unresolved", "ok"


def _plan_row(p, ptype, bu_id, brand_molecule_id, indication_id, country_id, overall,
              maps: ResolutionMaps) -> dict:
    """The target ``launch_plans`` row for one legacy plan."""
    row = {
        "id": det_id("plan", str(p.get("ID"))),
        "name": p.get("ProjectName"),
        "launch_type": "lep_launch_plan_tracker",
        "plan_type": ptype,
        "business_unit_id": bu_id,
        "brand_molecule_id": brand_molecule_id,
        "indication_id": indication_id,
        "country_id": country_id,
        "parent_plan_id": None,                           # 0.18: never populated
        "launch_lead_id": maps.user_id(p.get("Owner")),
        "health_status": plan_health_status(overall),
        "status": plan_status_from_overall(overall) or "active",
        "executive_summary": p.get("ExecutiveSummary"),
        "ccft_meeting_day": _day_of_month(p.get("CountryCustomerFacingTeamCCFTMeeting")),
        "is_deleted": bool(p.get("IsDeleted")),
        "is_private": bool(p.get("IsPrivate")),
        # NOT NULL with no DB default (the baseline is create_all, so the DDL
        # mirrors the model's python-side default and Postgres has nothing to fall
        # back on). Legacy carries no equivalent flag for either, so both take the
        # app's own default for a new plan.
        "is_key_assumptions_private": False,
        "is_dates_automation_enabled": False,
        # Legacy audit timestamps + author (FK-safe filtering done in load())
        "created_at": p.get("CreatedDate"),
        "updated_at": p.get("LastUpdatedDate"),
        "created_by": user_id_for_email((p.get("CreatedBy") or "").strip().lower()) or None,
        "updated_by": user_id_for_email((p.get("LastUpdatedBy") or "").strip().lower()) or None,
    }
    for src_col, tgt_col in DATE_MAP.items():
        row[tgt_col] = p.get(src_col)   # SQLAlchemy returns date/datetime; cast on write
    return row


def _prepare_plan(p, maps: ResolutionMaps, lookups):
    """Classify one legacy ``Projects`` row.

    Returns ``(outcome, payload)`` -- the row dict for ``_OK``, the reporting tuple
    for ``_UNRESOLVED``, ``None`` otherwise.
    """
    bu_by_code, countries_by_name, indications, brands_by_name, valid_bm_ids = lookups

    if p.get("IsTemplate"):
        return _TEMPLATE, None   # -> launch_templates (0.8)

    ptype = classify_plan_type(p.get("IsTemplate"), p.get("IsGlobalPlan"), p.get("IsMCEPlan"))

    # --- brand / molecule (family key; V20 verified consistent) ---
    fbi = maps.entry_name(p.get("FranchiseBrandIndication"))
    ta, _brand, indication = _split_fbi(fbi)
    # Prefer target brand_molecule_id (by brand name) over det_id — the det_id
    # depends on the TA string in the FBI, which varies across legacy entries for
    # the same brand; the target lookup is authoritative.
    brand_molecule_id = brands_by_name.get((_brand or "").lower()) or _brand_molecule_id(fbi)
    # Validate: det_id fallback may produce a UUID not in the target seed
    if brand_molecule_id and brand_molecule_id not in valid_bm_ids:
        brand_molecule_id = None

    # --- scope filter (migration brands; --full / LEP_MIGRATION_SCOPE=full overrides) ---
    # Skipping here cascades to every child loader, which all gate on the plans
    # load_plans actually wrote (SELECT id FROM lep.launch_plans).
    if not in_scope(fbi):
        return _OUT_OF_SCOPE, None

    # --- country (RegionCountry entry = 'Region.Country') ---
    rc = maps.entry_name(p.get("RegionCountry"))
    _, country = split_region_country(rc)
    cname = (canonical_country(country) or "").lower()

    # --- country + business unit together (GEO-3 / GEO-5) ---
    # BU first, then the country row for that BU: several BUs manage the same
    # market, so name alone does not identify a row.
    country_id, bu_id = _resolve_geography(
        ptype, ta, country, countries_by_name.get(cname, []), bu_by_code)

    overall = maps.entry_name(p.get("OverallLaunchStatus"))

    if not (brand_molecule_id and country_id and bu_id):
        return _UNRESOLVED, (str(p.get("ID")), ptype, fbi, rc)

    # --- indication (third leg of the launch uniqueness key) ---
    # The legacy FBI entry carries it; resolve against the seeded indications for
    # this brand. NULL only when the legacy entry has no indication segment or the
    # brand has no such indication seeded.
    indication_id = indications.get((brand_molecule_id, indication.lower())) if indication else None
    # Indication alias fallback (e.g. "PV" → "Polycythemia Vera")
    if indication_id is None and indication and brand_molecule_id:
        alias = _INDICATION_ALIASES.get(indication.lower())
        if alias:
            indication_id = indications.get((brand_molecule_id, alias))

    return _OK, _plan_row(p, ptype, bu_id, brand_molecule_id, indication_id, country_id,
                          overall, maps)


def _upsert_plans(target: Engine, rows: list[dict]) -> None:
    cols = sorted({k for r in rows for k in r})
    collist = ", ".join(cols)
    params = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    upsert = text(
        f"INSERT INTO lep.launch_plans ({collist}) VALUES ({params}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}"
    )
    with target.begin() as conn:
        for r in rows:
            conn.execute(upsert, {c: r.get(c) for c in cols})


def load(source: Engine, target: Engine, maps: ResolutionMaps, *, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    bu_by_code, _bu_code_by_id, countries_by_name, indications, brands_by_name, valid_bm_ids = _target_maps(target)
    lookups = (bu_by_code, countries_by_name, indications, brands_by_name, valid_bm_ids)
    rows, unresolved = [], []
    skipped = {_TEMPLATE: 0, _OUT_OF_SCOPE: 0}
    no_indication = 0
    print(f"[load_plans] scope = {scope_label()}")

    with source.connect() as conn:
        for p in conn.execute(_SRC).mappings():
            outcome, payload = _prepare_plan(p, maps, lookups)
            if outcome == _OK:
                rows.append(payload)
                if payload["indication_id"] is None:
                    no_indication += 1
            elif outcome == _UNRESOLVED:
                unresolved.append(payload)
            else:
                skipped[outcome] += 1

    skipped_template, out_of_scope = skipped[_TEMPLATE], skipped[_OUT_OF_SCOPE]
    if unresolved:
        print(f"[load_plans] WARNING {len(unresolved)} plans unresolved (brand/country/BU); "
              f"first: {unresolved[:3]}")
    if no_indication:
        print(f"[load_plans] WARNING {no_indication}/{len(rows)} plans have no resolvable "
              "indication -> indication_id NULL (duplicate detection cannot see them)")
    # FK-safe: user FK columns must reference a loaded user.
    # maps.user_id() returns a deterministic uuid5 which may differ from the
    # actual ID in lep.users (e.g. users provisioned by the app via Entra ID
    # before the migration ran).  Remap det-id -> actual-id via email.
    if not dry_run:
        with target.connect() as c:
            _det_to_actual: dict[str, str] = {}
            for r in c.execute(text("SELECT id, email FROM lep.users")):
                _det_to_actual[user_id_for_email(r.email)] = str(r.id)
        for r in rows:
            for col in ("launch_lead_id", "created_by", "updated_by"):
                uid = r.get(col)
                if uid:
                    r[col] = _det_to_actual.get(uid)  # None if no match

    if dry_run:
        if _dry_state is not None:
            _dry_state["plan_ids"] = {r["id"] for r in rows}
            _dry_state["plan_types"] = {r["id"]: r["plan_type"] for r in rows}
        print(f"[load_plans] DRY RUN: {len(rows)} plans to load, "
              f"{skipped_template} templates skipped, {out_of_scope} out-of-scope, "
              f"{len(unresolved)} unresolved, {no_indication} without indication — no write")
        return len(rows)

    _upsert_plans(target, rows)
    print(f"[load_plans] upserted {len(rows)} launch_plans "
          f"({skipped_template} templates skipped, {out_of_scope} out-of-scope, "
          f"{len(unresolved)} unresolved)")
    return len(rows)


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    src = source_engine()
    load(src, target_engine(), ResolutionMaps.load(src), dry_run=args.dry_run)
