# Databricks notebook source
# MAGIC %md
# MAGIC # LEP data migration — Databricks (bronze → silver → gold)
# MAGIC
# MAGIC Source: Azure SQL `sqldb-lep-data-dev` (`lep.*`, system of record) via JDBC.
# MAGIC Target: **Lakebase** (managed Postgres) via JDBC/psycopg.
# MAGIC
# MAGIC This notebook ports the **verified** transforms from `scripts/migration/transforms.py`
# MAGIC (unit-tested reference) into PySpark. Logic matches guide §6.3 exactly — do not
# MAGIC re-derive. Blockers GEO-3 / 0.18 still gate the `plans` gold stage.

# COMMAND ----------
# MAGIC %md ## 0. Config (set as job/widget params or a secret scope)

# COMMAND ----------
dbutils.widgets.text("src_jdbc", "jdbc:sqlserver://sql-lep-data-dev.database.windows.net:1433;database=sqldb-lep-data-dev;encrypt=true")
dbutils.widgets.text("src_user", "")
dbutils.widgets.text("src_password", "")   # prefer dbutils.secrets.get(scope, key)
dbutils.widgets.text("lakebase_host", "")
dbutils.widgets.text("lakebase_db", "lep")
dbutils.widgets.text("lakebase_user", "")
dbutils.widgets.text("lakebase_password", "")

SRC = {"url": dbutils.widgets.get("src_jdbc"),
       "user": dbutils.widgets.get("src_user"),
       "password": dbutils.widgets.get("src_password"),
       "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver"}
LB_HOST = dbutils.widgets.get("lakebase_host")
LB_DB = dbutils.widgets.get("lakebase_db")
LB_USER = dbutils.widgets.get("lakebase_user")
LB_PW = dbutils.widgets.get("lakebase_password")
LB_JDBC = f"jdbc:postgresql://{LB_HOST}:5432/{LB_DB}?sslmode=require"

# COMMAND ----------
# MAGIC %md ## 1. Verified transforms (ported from scripts/migration/transforms.py, §6.3)

# COMMAND ----------
import uuid
from pyspark.sql import functions as F, types as T

NS = uuid.UUID("6b1d1f2e-0000-4c00-8000-1e9000000000")  # same namespace as the seed

def _det_id(*parts):
    return str(uuid.uuid5(NS, "|".join((p or "").strip().lower() for p in parts)))

det_id = F.udf(lambda *p: _det_id(*p), T.StringType())
user_id_for_email = F.udf(lambda e: _det_id("user", e) if e else None, T.StringType())

# --- plan_type (T10 / §3.5): IsTemplate wins, then MCE, then Global ---
def _plan_type(is_template, is_global, is_mce):
    if is_template: return "template"
    if is_mce: return "mce"
    if is_global: return "global"
    return "local"
plan_type = F.udf(_plan_type, T.StringType())

# --- OverallLaunchStatus -> health_status (§6.3-A) ---
_HEALTH = {"green": "healthy", "amber": "at_risk", "red": "critical", "completed": None}
health_status = F.udf(lambda s: _HEALTH.get((s or "").strip().lower()), T.StringType())
plan_status_from_overall = F.udf(
    lambda s: "completed" if (s or "").strip().lower() == "completed" else None, T.StringType())

# --- LepStatus -> workplan_items.status (§6.3-B, clean 1:1; Green anomaly -> not_started) ---
_TASK_STATUS = {"not started": "not_started", "in progress": "in_progress",
                "delayed": "delayed", "completed": "completed", "green": "not_started"}
task_status = F.udf(lambda s: _TASK_STATUS.get((s or "").strip().lower(), "not_started"), T.StringType())

# --- Tasks.Status (RAG) -> is_at_risk (§6.3-C) ---
task_is_at_risk = F.udf(lambda s: (s or "").strip().lower() in ("amber", "red"), T.BooleanType())

# --- country alias (GEO-4) + region.country split ---
COUNTRY_ALIAS = {"greater ecuador": "Ecuador", "carribean": "Caribbean",
                 "greater china": "China", "greator ecuador": "Ecuador", "macedonia": "North Macedonia"}
canonical_country = F.udf(
    lambda c: COUNTRY_ALIAS.get((c or "").strip().lower(), (c or "").strip()) if c else None, T.StringType())
# 'Takeda Region Country LT' entries are 'Region.Country'
region_of = F.udf(lambda n: (n.split(".", 1)[0].strip() if n else None), T.StringType())
country_of = F.udf(lambda n: (n.split(".", 1)[1].strip() if n and "." in n else None), T.StringType())

normalize_function = F.udf(
    lambda n: " ".join(n.replace("/ ", "/").split()) if n else None, T.StringType())

# COMMAND ----------
# MAGIC %md ## 2. Bronze — read source `lep.*` via JDBC

# COMMAND ----------
def bronze(table):
    return (spark.read.format("jdbc")
            .option("url", SRC["url"]).option("user", SRC["user"]).option("password", SRC["password"])
            .option("driver", SRC["driver"]).option("dbtable", f"lep.{table}").load())

# COMMAND ----------
# MAGIC %md ## 3. Resolution maps (LookupEntries / Resources) — broadcast joins (T23/T24)

# COMMAND ----------
lookups = bronze("Lookups").select(F.col("ID").alias("lk_id"), F.col("Name").alias("category"))
lookup_entries = (bronze("LookupEntries")
    .select(F.col("ID").alias("le_id"), F.col("Lookup").alias("lk_id"),
            F.col("Name").alias("entry_name"), F.col("Active").alias("le_active"))
    .join(F.broadcast(lookups), "lk_id", "left"))
resources = bronze("Resources").select(
    F.col("ID").alias("res_id"),
    F.lower(F.trim(F.col("ResourceEmailAddress"))).alias("email"))

def resolve_entry(df, guid_col, out_col):
    """GUID -> LookupEntries.Name (T23)."""
    return (df.join(F.broadcast(lookup_entries.select("le_id", F.col("entry_name").alias(out_col))),
                    df[guid_col] == F.col("le_id"), "left").drop("le_id"))

def resolve_user(df, guid_col, out_col):
    """GUID -> Resources.email -> users.id (T24)."""
    r = resources.select("res_id", F.col("email"))
    return (df.join(F.broadcast(r), df[guid_col] == F.col("res_id"), "left")
              .withColumn(out_col, user_id_for_email(F.col("email"))).drop("res_id", "email"))

# COMMAND ----------
# MAGIC %md ## 4. Gold writer — stage to Lakebase, then INSERT ... ON CONFLICT (idempotent)

# COMMAND ----------
def upsert_lakebase(df, target_table, conflict_cols, update_cols):
    """Spark JDBC can't upsert. Write to a staging table, then MERGE via ON CONFLICT.
    Idempotent because ids are uuid5 (deterministic)."""
    import psycopg
    stg = f"{target_table.split('.')[-1]}_stg"
    (df.write.format("jdbc").mode("overwrite")
       .option("url", LB_JDBC).option("user", LB_USER).option("password", LB_PW)
       .option("driver", "org.postgresql.Driver").option("dbtable", f"lep.{stg}").save())
    cols = df.columns
    collist = ", ".join(cols)
    setlist = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    conflict = ", ".join(conflict_cols)
    sql = (f"INSERT INTO {target_table} ({collist}) SELECT {collist} FROM lep.{stg} "
           f"ON CONFLICT ({conflict}) DO UPDATE SET {setlist}; DROP TABLE lep.{stg};")
    with psycopg.connect(host=LB_HOST, dbname=LB_DB, user=LB_USER, password=LB_PW, sslmode="require") as c:
        c.execute(sql); c.commit()
    print(f"upserted -> {target_table}")

# COMMAND ----------
# MAGIC %md ## 5. Worked example — `Projects` -> `launch_plans`
# MAGIC ⚠️ Gated on **GEO-3** (Global-plan BU: OBU/SPD) and **0.18** (dual parent columns).
# MAGIC Leave commented until both are signed off; wire the resolved FKs where marked TODO.

# COMMAND ----------
p = bronze("Projects")
# filter templates out (IsTemplate=1 -> launch_templates, decision 0.8)
p = p.filter(F.coalesce(F.col("IsTemplate"), F.lit(False)) == False)  # noqa: E712
p = (p
     .withColumn("plan_type", plan_type(F.col("IsTemplate"), F.col("IsGlobalPlan"), F.col("IsMCEPlan")))
     .withColumn("id", det_id(F.lit("plan"), F.col("ID").cast("string")))
     .withColumn("is_deleted", F.coalesce(F.col("IsDeleted"), F.lit(False))))
p = resolve_entry(p, "RegionCountry", "region_country")          # 'Region.Country'
p = p.withColumn("country_name", canonical_country(country_of(F.col("region_country"))))
p = resolve_entry(p, "OverallLaunchStatus", "overall_status")
p = p.withColumn("health_status", health_status(F.col("overall_status")))
p = resolve_user(p, "Owner", "launch_lead_id")
# TODO (blocked): resolve country_name -> country_id/business_unit_id via the seeded lep.countries
#                 (GEO-3: for plan_type='global' take BU from plan context OBU/SPD, NOT the Global country's IBU)
# TODO (0.18):    parent_plan_id from MCEPlanGUID/GlobalPlanGuid; add global_plan_id + mce_plan_id once schema lands
# gold = p.select("id","name","plan_type","business_unit_id","country_id","brand_molecule_id",
#                 "launch_lead_id","health_status","is_deleted", ...dates...)
# upsert_lakebase(gold, "lep.launch_plans", ["id"], [c for c in gold.columns if c != "id"])
display(p.select("id", "plan_type", "country_name", "health_status", "launch_lead_id").limit(20))

# COMMAND ----------
# MAGIC %md
# MAGIC ### Next stages (same pattern)
# MAGIC - `users` from `Resources` (unblocked — mirror `load_users.py`): id=`user_id_for_email(email)`, upsert on email.
# MAGIC - `workplan_items` from `Tasks` (200K): `task_status(LepStatus)`, `task_is_at_risk(Status)`, parent-before-child.
# MAGIC - memberships / children / trackers / notifications — resolve FKs, upsert in FK-safe order.
# MAGIC - Validate on the Lakebase branch (V1–V19), then discard the branch.
