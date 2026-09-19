"""Load plan team into ``lep.plan_memberships``.

Sources: ``ProjectResources`` (-> team_member; role=function is dropped per 0.14),
``SecondaryLaunchLeads`` (-> secondary_launch_lead). Empty in the dump but kept:
``RegionCountryLeads`` / ``FranchiseLeads`` (-> team_member).

The launch lead is NOT loaded as a membership — ``launch_plans.launch_lead_id``
(set by ``load_plans``) is the sole source of truth for plan ownership.  Creating
a ``launch_lead`` role membership would cause the app to show the LL under both
the "Launch Lead" section and the "Team Members" section.

Post-processing dedup:
  - If a user is the plan's launch lead, any team_member or secondary_launch_lead
    membership for that user on that plan is dropped (owner ≠ co-owner or member).
  - If a user is already an SLL on a plan, any team_member membership for that
    user on that plan is dropped (SLL ≠ generic member).

Idempotent on (plan, user, role): ``id = uuid5('membership', plan, user, role)``.
Skips rows whose plan wasn't loaded or whose user can't resolve.
"""
from __future__ import annotations

import argparse

from sqlalchemy import Engine, text

from scripts.migration.resolution_maps import ResolutionMaps
from scripts.migration.transforms import det_id, user_id_for_email


def load(source: Engine, target: Engine, maps: ResolutionMaps, *, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    with target.connect() as c:
        roles = {r.code: str(r.id) for r in c.execute(text("SELECT code, id FROM lep.roles"))}
        if dry_run and _dry_state and "plan_ids" in _dry_state:
            plans = _dry_state["plan_ids"]
        else:
            plans = {str(r.id) for r in c.execute(text("SELECT id FROM lep.launch_plans"))}

    seen: set[str] = set()
    rows: list[dict] = []

    def add(plan_guid, user_guid, role_code, active=True):
        plan_id = det_id("plan", str(plan_guid))
        user_id = maps.user_id(user_guid)
        role_id = roles.get(role_code)
        if plan_id not in plans or not user_id or not role_id:
            return
        mid = det_id("membership", plan_id, user_id, role_code)
        if mid in seen:
            return
        seen.add(mid)
        rows.append({"id": mid, "launch_plan_id": plan_id, "user_id": user_id,
                     "role_id": role_id, "is_deleted": not bool(active)})

    with source.connect() as conn:
        for r in conn.execute(text('SELECT "Project", "Resource", "Active" FROM lep."ProjectResources"')):
            add(r.Project, r.Resource, "team_member", r.Active)
        for r in conn.execute(text('SELECT "Project", "Resource" FROM lep."SecondaryLaunchLeads"')):
            add(r.Project, r.Resource, "secondary_launch_lead")
        # NOTE: launch_lead membership deliberately NOT created here.
        # launch_plans.launch_lead_id (set by load_plans) is the sole source of
        # truth.  A membership row would duplicate the LL in Team Members.
        # RegionCountryLeads/FranchiseLeads DEFERRED: the DMS source tables key on
        # RegionCountry/Franchise, not Project — they need a different join. Skip
        # until confirmed; core memberships come from ProjectResources + leads above.

    # --- Mark redundant memberships as soft-deleted ---
    # In the legacy system a user could appear as Owner (launch_lead),
    # SecondaryLaunchLead, AND a ProjectResource (team_member) on the same plan.
    # In the new app:
    #   - launch_lead is stored on launch_plans.launch_lead_id, not as a membership
    #   - SLLs should not also appear as team_members (shown in both sections)
    #   - The LL should not also appear as an SLL (owner being co-owner is redundant)
    # We keep the rows (for idempotent upserts) but mark them is_deleted=True.
    ll_pairs = {
        (r["launch_plan_id"], r["user_id"])
        for r in rows if r["role_id"] == roles.get("launch_lead")
    }
    sll_pairs = {
        (r["launch_plan_id"], r["user_id"])
        for r in rows if r["role_id"] == roles.get("secondary_launch_lead")
    }
    redundant = 0
    for r in rows:
        pair = (r["launch_plan_id"], r["user_id"])
        if (
            # launch_lead membership: redundant with launch_plans.launch_lead_id
            r["role_id"] == roles.get("launch_lead")
            # team_member where user is the LL on this plan
            or (r["role_id"] == roles.get("team_member") and pair in ll_pairs)
            # team_member where user is already an SLL on this plan
            or (r["role_id"] == roles.get("team_member") and pair in sll_pairs)
            # SLL where user is already the LL on this plan
            or (r["role_id"] == roles.get("secondary_launch_lead") and pair in ll_pairs)
        ):
            r["is_deleted"] = True
            redundant += 1
    if redundant:
        print(f"[load_memberships] marked {redundant} redundant memberships as soft-deleted")

    if dry_run:
        print(f"[load_memberships] DRY RUN: {len(rows)} memberships — no write")
        return len(rows)

    # FK-safe: remap user_id det-uuid5 -> actual DB id; drop unresolvable.
    with target.connect() as c:
        _det_to_actual: dict[str, str] = {}
        for r in c.execute(text("SELECT id, email FROM lep.users")):
            _det_to_actual[user_id_for_email(r.email)] = str(r.id)
    for r in rows:
        r["user_id"] = _det_to_actual.get(r["user_id"])
    before = len(rows)
    rows = [r for r in rows if r["user_id"]]
    dropped = before - len(rows)
    if dropped:
        print(f"[load_memberships] dropped {dropped} memberships (user not in target)")

    # ── Dedup: remove redundant role overlaps ────────────────────────────
    # 1. Remove any membership (TM or SLL) where user is the plan's launch lead
    with target.connect() as c:
        plan_lead = {str(r[0]): str(r[1]) for r in c.execute(text(
            "SELECT id, launch_lead_id FROM lep.launch_plans WHERE launch_lead_id IS NOT NULL"
        ))}
    before_dedup = len(rows)
    rows = [r for r in rows if not (
        r["launch_plan_id"] in plan_lead
        and r["user_id"] == plan_lead[r["launch_plan_id"]]
    )]
    ll_dropped = before_dedup - len(rows)
    if ll_dropped:
        print(f"[load_memberships] dropped {ll_dropped} memberships (user is launch lead)")

    # 2. Remove team_member where user is already SLL on the same plan
    sll_pairs = {
        (r["launch_plan_id"], r["user_id"])
        for r in rows
        if r["role_id"] == roles.get("secondary_launch_lead")
    }
    before_sll = len(rows)
    rows = [r for r in rows if not (
        r["role_id"] == roles.get("team_member")
        and (r["launch_plan_id"], r["user_id"]) in sll_pairs
    )]
    sll_dropped = before_sll - len(rows)
    if sll_dropped:
        print(f"[load_memberships] dropped {sll_dropped} team_member memberships (user is SLL)")

    cols = ["id", "launch_plan_id", "user_id", "role_id", "is_deleted"]
    upsert = text(
        f"INSERT INTO lep.plan_memberships ({', '.join(cols)}) "
        f"VALUES ({', '.join(':'+c for c in cols)}) "
        f"ON CONFLICT (launch_plan_id, user_id, role_id) DO UPDATE SET is_deleted = EXCLUDED.is_deleted"
    )
    with target.begin() as conn:
        for i in range(0, len(rows), 5000):
            conn.execute(upsert, rows[i:i + 5000])
    print(f"[load_memberships] upserted {len(rows)} memberships")
    return len(rows)


if __name__ == "__main__":
    from scripts.migration.db import source_engine, target_engine
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(); s = source_engine()
    load(s, target_engine(), ResolutionMaps.load(s), dry_run=a.dry_run)
