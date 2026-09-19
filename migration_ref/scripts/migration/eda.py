"""LEP migration — Exploratory Data Analysis (EDA), single file.

Consolidates every ad-hoc analysis run during the 2026-07-30 verification of the
legacy BACPAC dump (`Lep_dump_out/`) against the new requirement-aligned taxonomy
(`scripts/seed_bu_geography.sql`). Self-contained — includes both readers:
  * `dbo.*` native .BCP reader (reliable)
  * `lep.*` tolerant length-prefixed reader (FIND-2: mis-frames in the naive reader)

NOTE: for authoritative transactional counts/values, read the live SQL Server via
`verify-legacy.sql` — the `.BCP` readers here are for offline exploration only.

Usage:
    python -m scripts.migration.eda all          # run everything
    python -m scripts.migration.eda countries     # legacy vs new country cross-check
    python -m scripts.migration.eda hierarchy resolve lookups plantype stamp anomalies
"""
from __future__ import annotations

import os
import re
import struct
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
DUMP = os.environ.get("LEP_DUMP_DIR", os.path.join(REPO, "Lep_dump_out"))
SEED = os.path.join(HERE, "..", "seed_bu_geography.sql")
MODEL = os.path.join(DUMP, "model.xml")


# ------------------------------------------------------------------ readers
_FIXED = {"bigint": 8, "int": 4, "smallint": 2, "tinyint": 1, "bit": 1,
          "datetime": 8, "uniqueidentifier": 16, "float": 8, "real": 4,
          "money": 8, "smalldatetime": 4, "date": 3}
_VARLEN = {"nvarchar", "varchar", "varbinary", "nchar", "char", "sysname"}


def _model_xml() -> str:
    with open(MODEL, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def model_columns(schema: str, table: str):
    """(name, builtin_type, nullable) for a table, from model.xml."""
    xml = _model_xml()
    pat = (r'<Element Type="SqlSimpleColumn" Name="\[%s\]\.\[%s\]\.\[([^\]]+)\]">(.*?)</Element>'
           % (schema, re.escape(table)))
    out = []
    for m in re.finditer(pat, xml, re.S):
        bt = re.search(r'References ExternalSource="BuiltIns" Name="\[([^\]]+)\]"', m.group(2))
        if not bt:
            continue
        nn = 'Name="IsNullable" Value="False"' in m.group(2)
        out.append((m.group(1), bt.group(1).lower(), not nn))
    return out


def _table_bytes(schema: str, table: str) -> bytes:
    with open(os.path.join(DUMP, "Data", f"{schema}.{table}",
                           "TableData-000-00000.BCP"), "rb") as fh:
        return fh.read()


def _read_varlen_dbo(data, i, bt):
    """Length-prefixed (2B) value -> ``(value, next_i)``; 0xFFFF is the null marker.
    ``n*``/``sysname`` are UTF-16, everything else latin1."""
    ln = struct.unpack_from("<H", data, i)[0]; i += 2
    if ln == 0xFFFF:
        return None, i
    raw = data[i:i + ln]; i += ln
    return (raw.decode("utf-16-le", "replace") if bt[0] in "ns"
            else raw.decode("latin1")), i


def _coerce_dbo(raw, bt):
    if bt in ("bigint", "int", "smallint", "tinyint"):
        return int.from_bytes(raw, "little", signed=True)
    if bt == "bit":
        return bool(raw[0])
    return raw


def _read_fixed_dbo(data, i, bt, nullable):
    """Fixed-width value -> ``(value, next_i)``. NULLable columns carry a 1B length
    prefix (0xFF = null); NOT NULL columns are raw bytes of the type's width."""
    if nullable:
        ln = data[i]; i += 1
        if ln == 0xFF:
            return None, i
        raw = data[i:i + ln]; i += ln
    else:
        size = _FIXED.get(bt, 0)
        raw = data[i:i + size]; i += size
    return _coerce_dbo(raw, bt), i


def read_dbo(table: str):
    """Native .BCP reader for dbo.* (reliable). Fixed NOT-NULL = raw bytes."""
    cols = model_columns("dbo", table)
    data = _table_bytes("dbo", table)
    i, n, rows = 0, len(data), []
    while i < n:
        row = {}
        for name, bt, nullable in cols:
            if bt in _VARLEN:
                row[name], i = _read_varlen_dbo(data, i, bt)
            else:
                row[name], i = _read_fixed_dbo(data, i, bt, nullable)
        rows.append(row)
    return rows


def _coerce_lep(raw, bt):
    if bt == "uniqueidentifier":
        return raw.hex()
    if bt == "bit":
        return bool(raw[0]) if raw else None
    if bt == "float":
        return struct.unpack("<d", raw)[0] if len(raw) == 8 else None
    return raw


def _read_lep_value(data, i, bt):
    """One lep.* value -> ``(value, next_i)``. Every column carries a length prefix:
    2B for the varlen types (always UTF-16 here), 1B otherwise (0xFF = null)."""
    if bt in _VARLEN:
        ln = struct.unpack_from("<H", data, i)[0]; i += 2
        if ln == 0xFFFF:
            return None, i
        return data[i:i + ln].decode("utf-16-le", "replace"), i + ln
    ln = data[i]; i += 1
    if ln == 0xFF:
        return None, i
    raw = data[i:i + ln]; i += ln
    return _coerce_lep(raw, bt), i


def read_lep(table: str):
    """Tolerant reader for lep.* — every column carries a length prefix (nvarchar=2B,
    else 1B). Stops at the first row that runs off the end of the buffer (FIND-2:
    lep.* mis-frames, so a short read is expected rather than exceptional)."""
    cols = model_columns("lep", table)
    data = _table_bytes("lep", table)
    i, n, rows = 0, len(data), []
    while i < n:
        row, ok = {}, True
        for name, bt, _nullable in cols:
            if i >= n:
                ok = False
                break
            row[name], i = _read_lep_value(data, i, bt)
        if not ok:
            break
        rows.append(row)
    return rows


# ------------------------------------------------------- new taxonomy (seed)
def new_taxonomy():
    """Parse seed_bu_geography.sql -> (bu{id:code}, area{id:name}, clus{id:name},
    geo{country_lower:(bu,area,cluster)}, names{country_names})."""
    with open(SEED) as fh:
        sql = fh.read()
    bu = {m.group(1): m.group(2) for m in re.finditer(
        r"business_units \(id, code, name, is_active\) VALUES \('([^']+)'::uuid, '([^']+)'", sql)}
    area = {m.group(1): m.group(4) for m in re.finditer(
        r"areas \(id, business_unit_id, code, name, is_active\) VALUES \('([^']+)'::uuid, '([^']+)'::uuid, '([^']+)', '([^']+)'", sql)}
    clus = {m.group(1): m.group(5) for m in re.finditer(
        r"clusters \(id, business_unit_id, area_id, code, name, is_active\) VALUES \('([^']+)'::uuid, '([^']+)'::uuid, '([^']+)'::uuid, '([^']+)', '([^']+)'", sql)}
    geo, names = {}, set()
    for line in sql.splitlines():
        if "INSERT INTO lep.countries" not in line:
            continue
        mm = re.search(r"VALUES \('([0-9a-f-]+)'::uuid, '([0-9a-f-]+)'::uuid, '([0-9a-f-]+)'::uuid, (NULL|'[0-9a-f-]+'::uuid), '([^']*)', '(.*?)', (TRUE|FALSE)\)", line)
        if not mm:
            continue
        cid, buid, aid, clfield, iso, name, _ = mm.groups()
        name = name.replace("''", "'")
        clid = None if clfield == "NULL" else clfield.split("'")[1]
        geo[name.lower()] = (bu.get(buid), area.get(aid), clus.get(clid) if clid else None)
        names.add(name)
    return bu, area, clus, geo, names


# --------------------------------------------------------------- EDA sections
def eda_legacy_geography():
    print("\n########## LEGACY GEOGRAPHY (dbo.Projects + CountryWaveMapping) ##########")
    proj = read_dbo("Projects")
    regions = sorted({r["Region"].strip() for r in proj if r.get("Region")})
    countries = sorted({r["Country"].strip() for r in proj if r.get("Country")})
    clusters = {r.get("Cluster") for r in proj if r.get("Cluster")}
    print(f"dbo.Projects rows: {len(proj)} | distinct Region: {len(regions)} | "
          f"distinct Country: {len(countries)} | Cluster distinct: {len(clusters)} (empty!)")
    print("Regions:", regions)
    cwm = read_dbo("CountryWaveMapping")
    cwm_all = {r["CountryValue"].strip() for r in cwm if r.get("CountryValue")}
    print(f"dbo.CountryWaveMapping rows: {len(cwm)} | distinct countries: {len(cwm_all)}")


def eda_country_crosscheck():
    print("\n########## COUNTRY CROSS-CHECK (legacy vs new taxonomy) ##########")
    _, _, _, _, new_names = new_taxonomy()
    proj = read_dbo("Projects")
    cwm = read_dbo("CountryWaveMapping")
    legacy = {r["CountryValue"].strip() for r in cwm if r.get("CountryValue")}
    legacy |= {r["Country"].strip() for r in proj if r.get("Country")}
    norm = lambda s: s.strip().lower().replace("`", "'")
    cur = {norm(n): n for n in new_names}
    leg = {norm(n): n for n in legacy}
    print(f"new taxonomy: {len(new_names)} countries | legacy union: {len(legacy)}")
    print("In CURRENT not legacy:", sorted(cur[k] for k in cur if k not in leg))
    print("In LEGACY not current:", sorted(leg[k] for k in leg if k not in cur))


def eda_hierarchy():
    print("\n########## NEW DB HIERARCHY (seed) ##########")
    bu, area, clus, geo, names = new_taxonomy()
    print(f"BUs: {sorted(bu.values())}")
    print(f"areas: {len(area)} | clusters: {len(clus)} | countries: {len(names)}")
    per_bu = Counter(v[0] for v in geo.values())
    per_area = Counter(v[1] for v in geo.values())
    print("countries per BU:", dict(per_bu))
    print("countries per area:", dict(per_area))
    print("no-cluster countries:", sum(1 for v in geo.values() if v[2] is None))


def eda_anomalies():
    print("\n########## STRUCTURAL ANOMALIES ##########")
    _, _, clus, geo, _ = new_taxonomy()
    dup = {k: v for k, v in Counter(clus.values()).items() if v > 1}
    print("Duplicate cluster names across areas:", dup)
    per_bu = Counter(v[0] for v in geo.values())
    print("BU distribution (empty BUs are a smell):", dict(per_bu))


def eda_resolve_plans():
    print("\n########## RESOLVE LEGACY PLANS -> NEW BU/AREA/CLUSTER (by country) ##########")
    _, _, _, geo, _ = new_taxonomy()
    ALIAS = {"greater china": "china", "greator ecuador": "ecuador", "greater ecuador": "ecuador",
             "macedonia": "north macedonia", "carribean": "caribbean"}
    proj = read_dbo("Projects")
    used = Counter(r.get("Country") for r in proj if r.get("Country"))
    ok, blocked = 0, []
    for ctry, cnt in sorted(used.items(), key=lambda x: -x[1]):
        key = ALIAS.get(ctry.strip().lower(), ctry.strip().lower())
        hit = geo.get(key)
        if hit:
            ok += cnt
        else:
            blocked.append((ctry, cnt))
    print(f"resolvable plan-country rows: {ok} | blocked: {blocked}")


def eda_lookups():
    print("\n########## lep.Lookups / LookupEntries ##########")
    lk = read_lep("Lookups")
    id2name = {r["ID"]: r["Name"] for r in lk}
    le = read_lep("LookupEntries")
    print(f"Lookups: {len(lk)} categories | LookupEntries: {len(le)} entries")
    cnt = Counter(id2name.get(r["Lookup"]) for r in le if r.get("Lookup"))
    for cat, c in cnt.most_common():
        print(f"  {c:5}  {cat!r}")


def eda_plantype():
    print("\n########## PLAN-TYPE DISCRIMINATOR COLUMNS (lep.Projects) ##########")
    cols = dict((n, (t, nul)) for n, t, nul in model_columns("lep", "Projects"))
    for c in ["ProjectType", "IsGlobalPlan", "IsMCEPlan", "IsTemplate",
              "GlobalPlanGuid", "MCEPlanGUID", "IsTrackerOnly", "RegionCountry"]:
        print(f"  {c:18} -> {cols.get(c, '(absent)')}")
    print("Rule (T10): IsTemplate=1->template; IsMCEPlan=1->mce; IsGlobalPlan=1->global; else local")


def eda_stamp():
    print("\n########## COLUMN COUNTS (for Critical/Needed/Not-needed stamping) ##########")
    for sch, t in [("lep", "Projects"), ("lep", "Tasks"), ("lep", "Assignments"),
                   ("lep", "Dependencies"), ("lep", "ProjectResources"), ("lep", "Resources")]:
        print(f"  {sch}.{t}: {len(model_columns(sch, t))} columns")


SECTIONS = {
    "legacy": eda_legacy_geography, "countries": eda_country_crosscheck,
    "hierarchy": eda_hierarchy, "anomalies": eda_anomalies, "resolve": eda_resolve_plans,
    "lookups": eda_lookups, "plantype": eda_plantype, "stamp": eda_stamp,
}


def main(argv):
    picks = argv or ["all"]
    if "all" in picks:
        picks = list(SECTIONS)
    for p in picks:
        fn = SECTIONS.get(p)
        if fn:
            fn()
        else:
            print(f"unknown section {p!r}; choose from: all, {', '.join(SECTIONS)}")


if __name__ == "__main__":
    main(sys.argv[1:])
