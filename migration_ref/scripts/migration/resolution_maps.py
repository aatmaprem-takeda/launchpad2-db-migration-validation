"""In-memory resolution dictionaries built from the legacy ``lep.*`` schema.

Almost every categorical column on ``lep.Projects`` / ``lep.Tasks`` /
``lep.ProjectResources`` is a ``uniqueidentifier`` FK into ``lep.LookupEntries``
(guide §3.3 / T23), and every person FK points at ``lep.Resources`` (T24). Load
these two dictionaries **once**, up front, and resolve every GUID through them.

    maps = ResolutionMaps.load(source_engine())
    maps.entry_name(guid)         -> 'Green'
    maps.entry_category(guid)     -> 'Overall Launch Status LT'
    maps.resource_email(guid)     -> 'jane@takeda.com'
    maps.user_id(guid)            -> det_id('user', email)  (target users.id)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from scripts.migration.transforms import user_id_for_email


@dataclass
class ResolutionMaps:
    # LookupEntries.ID(hex uppercased) -> (category_name, entry_name, active)
    entries: dict[str, tuple[str, str, bool]] = field(default_factory=dict)
    # Resources.ID(hex uppercased) -> email
    resources: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _key(guid: object) -> str | None:
        if guid is None:
            return None
        # SQL Server uniqueidentifier comes back as a str like '0566C6C9-...';
        # normalize to a bare uppercase hex key so lookups are case/format stable.
        return str(guid).replace("-", "").replace("{", "").replace("}", "").upper()

    @classmethod
    def load(cls, source: Engine) -> "ResolutionMaps":
        m = cls()
        with source.connect() as conn:
            for row in conn.execute(text(
                'SELECT le."ID", lk."Name" AS category, le."Name" AS entry, le."Active" '
                'FROM lep."LookupEntries" le JOIN lep."Lookups" lk ON lk."ID" = le."Lookup"'
            )):
                key = cls._key(row.ID)
                if key:
                    m.entries[key] = (row.category, row.entry, bool(row.Active))
            for row in conn.execute(text(
                'SELECT "ID", "ResourceEmailAddress" FROM lep."Resources"'
            )):
                key = cls._key(row.ID)
                email = (row.ResourceEmailAddress or "").strip().lower()
                if key and email:
                    m.resources[key] = email
        return m

    # -- resolvers -----------------------------------------------------------
    def entry_name(self, guid: object) -> str | None:
        rec = self.entries.get(self._key(guid) or "")
        return rec[1] if rec else None

    def entry_category(self, guid: object) -> str | None:
        rec = self.entries.get(self._key(guid) or "")
        return rec[0] if rec else None

    def resource_email(self, guid: object) -> str | None:
        return self.resources.get(self._key(guid) or "")

    def user_id(self, guid: object) -> str | None:
        email = self.resource_email(guid)
        return user_id_for_email(email) if email else None

    def summary(self) -> str:
        return f"{len(self.entries)} lookup entries, {len(self.resources)} resources"
