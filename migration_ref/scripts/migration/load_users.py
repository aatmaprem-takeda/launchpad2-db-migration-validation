"""Load ``lep.Resources`` (the SOR person pool) -> ``lep.users``.

This is the identity foundation the plan/task/assignment loaders depend on
(Owner, Assignments.Resource, ProjectResources.Resource all resolve to a user).
Verified 2026-07-30: 4,079 Resources rows -> 4,078 distinct emails (4,060 active);
0 unresolved owners/assignments (guide §7). No migration blocker.

Idempotent: ``id = det_id('user', email)`` and ``ON CONFLICT (email) DO UPDATE``,
matching seed_from_dump so seed + migration reconcile to one row per email.
"""
from __future__ import annotations

from sqlalchemy import Engine, text

from scripts.migration.transforms import user_id_for_email
from scripts.migration.scope import in_scope, is_full_migration

_SELECT = text(
    'SELECT "ResourceEmailAddress" AS email, "ResourceName" AS name, "Active" AS active '
    'FROM lep."Resources"'
)

_UPSERT = text(
    "INSERT INTO lep.users (id, email, display_name, is_active) "
    "VALUES (CAST(:id AS uuid), :email, :display_name, :is_active) "
    "ON CONFLICT (email) DO UPDATE SET "
    "display_name = EXCLUDED.display_name, is_active = EXCLUDED.is_active"
)


def _in_scope_emails(source: Engine, maps) -> set[str] | None:
    """Emails of users referenced by in-scope plans. None = load all (full scope)."""
    if is_full_migration():
        return None
    emails: set[str] = set()
    in_scope_projects: set[str] = set()
    with source.connect() as conn:
        # In-scope project IDs + plan owners
        for p in conn.execute(text(
                'SELECT "ID", "FranchiseBrandIndication", "IsTemplate", "Owner" '
                'FROM lep."Projects"')):
            if p.IsTemplate:
                continue
            fbi = maps.entry_name(p.FranchiseBrandIndication)
            if not in_scope(fbi):
                continue
            in_scope_projects.add(str(p.ID))
            email = maps.resource_email(p.Owner)
            if email:
                emails.add(email)
        # Team members
        for r in conn.execute(text(
                'SELECT "Project", "Resource" FROM lep."ProjectResources"')):
            if str(r.Project) in in_scope_projects:
                email = maps.resource_email(r.Resource)
                if email:
                    emails.add(email)
        # Secondary leads
        for r in conn.execute(text(
                'SELECT "Project", "Resource" FROM lep."SecondaryLaunchLeads"')):
            if str(r.Project) in in_scope_projects:
                email = maps.resource_email(r.Resource)
                if email:
                    emails.add(email)
        # Task assignees + audit authors (Assignments -> Tasks -> in-scope Projects)
        # CreatedBy / LastUpdatedBy are email addresses on the Tasks table;
        # provision them so workplan_items.created_by / updated_by resolve
        # instead of falling back to NULL.
        in_scope_tasks: set[str] = set()
        for t in conn.execute(text(
                'SELECT "ID", "ProjectID", "CreatedBy", "LastUpdatedBy" '
                'FROM lep."Tasks"')):
            if str(t.ProjectID) in in_scope_projects:
                in_scope_tasks.add(str(t.ID))
                for col in (t.CreatedBy, t.LastUpdatedBy):
                    if col and "@" in col:
                        emails.add(col.strip().lower())
        for a in conn.execute(text(
                'SELECT "Task", "Resource" FROM lep."Assignments"')):
            if str(a.Task) in in_scope_tasks:
                email = maps.resource_email(a.Resource)
                if email:
                    emails.add(email)
        # Notification recipients (avoids FK drops in load_notifications)
        for n in conn.execute(text(
                'SELECT "AssignedTo", "ProjectID" FROM lep."Notifications"')):
            if str(n.ProjectID) in in_scope_projects:
                email = maps.resource_email(n.AssignedTo)
                if email:
                    emails.add(email)
    return emails


def collect(source: Engine) -> dict[str, dict]:
    """Read Resources and dedupe by lowercased email (active wins)."""
    users: dict[str, dict] = {}
    with source.connect() as conn:
        for row in conn.execute(_SELECT):
            email = (row.email or "").strip().lower()
            if not email:
                continue
            name = (row.name or "").strip() or email
            active = bool(row.active)
            existing = users.get(email)
            if existing is None:
                users[email] = {
                    "id": user_id_for_email(email),
                    "email": email,
                    "display_name": name,
                    "is_active": active,
                }
            else:
                existing["is_active"] = existing["is_active"] or active
                if not existing["display_name"]:
                    existing["display_name"] = name
    return users


def load(source: Engine, target: Engine, *, maps=None, dry_run: bool = False, _dry_state: dict | None = None) -> int:
    users = collect(source)
    # Scope to users referenced by in-scope plans
    if maps is not None:
        scoped = _in_scope_emails(source, maps)
        if scoped is not None:
            users = {e: u for e, u in users.items() if e in scoped}
    if dry_run:
        active = sum(1 for u in users.values() if u["is_active"])
        print(f"[load_users] DRY RUN: {len(users)} distinct users ({active} active) — no write")
        return len(users)
    with target.begin() as conn:
        for u in users.values():
            conn.execute(_UPSERT, u)
    print(f"[load_users] upserted {len(users)} users")
    return len(users)
