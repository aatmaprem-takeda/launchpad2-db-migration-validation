# Databricks notebook source
# MAGIC %md
# MAGIC # LEP migration — EDA against the LIVE legacy Azure SQL
# MAGIC
# MAGIC Reads `sqldb-lep-data-dev` (`lep.*`, system of record) directly over JDBC —
# MAGIC the **authoritative** version of the exploration we did (closes FIND-2; the
# MAGIC offline `.BCP` reader in `scripts/migration/eda.py` is the file-based twin).
# MAGIC
# MAGIC Databricks connects to Azure SQL with the built-in SQL Server JDBC driver — no
# MAGIC library install. Put the password in a **secret scope**, never in the notebook.

# COMMAND ----------
# MAGIC %md ## 0. Connection

# COMMAND ----------
dbutils.widgets.text("host", "sql-lep-data-dev.database.windows.net")
dbutils.widgets.text("database", "sqldb-lep-data-dev")
dbutils.widgets.text("user", "LE-DBA-DEV")
dbutils.widgets.text("secret_scope", "lep-migration")
dbutils.widgets.text("secret_key", "src_password")

HOST = dbutils.widgets.get("host")
DATABASE = dbutils.widgets.get("database")
USER = dbutils.widgets.get("user")
# password from secret scope (fallback to a widget only for a quick manual test)
try:
    PW = dbutils.secrets.get(dbutils.widgets.get("secret_scope"), dbutils.widgets.get("secret_key"))
except Exception:
    dbutils.widgets.text("password", "")
    PW = dbutils.widgets.get("password")

URL = (f"jdbc:sqlserver://{HOST}:1433;database={DATABASE};"
       "encrypt=true;trustServerCertificate=false;loginTimeout=30")
JDBC = {"url": URL, "user": USER, "password": PW,
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver"}


def q(sql: str):
    """Run a (CTE-free) SELECT on the server and return a Spark DataFrame."""
    return (spark.read.format("jdbc")
            .option("url", JDBC["url"]).option("user", JDBC["user"])
            .option("password", JDBC["password"]).option("driver", JDBC["driver"])
            .option("query", sql).load())


def tbl(name: str):
    return (spark.read.format("jdbc")
            .option("url", JDBC["url"]).option("user", JDBC["user"])
            .option("password", JDBC["password"]).option("driver", JDBC["driver"])
            .option("dbtable", f"lep.{name}").load())


# quick connectivity check
display(q("SELECT DB_NAME() AS db, SUSER_SNAME() AS login, GETDATE() AS server_time"))

# COMMAND ----------
# MAGIC %md ## 1. Anchor row counts (verify-legacy §0)

# COMMAND ----------
display(q("""
SELECT 'Projects' AS tbl, COUNT(*) AS rows FROM lep.Projects
UNION ALL SELECT 'Tasks', COUNT(*) FROM lep.Tasks
UNION ALL SELECT 'TaskFunctions', COUNT(*) FROM lep.TaskFunctions
UNION ALL SELECT 'Assignments', COUNT(*) FROM lep.Assignments
UNION ALL SELECT 'Notifications', COUNT(*) FROM lep.Notifications
UNION ALL SELECT 'Resources', COUNT(*) FROM lep.Resources
UNION ALL SELECT 'TaskDocuments', COUNT(*) FROM lep.TaskDocuments
UNION ALL SELECT 'Tracker', COUNT(*) FROM lep.Tracker
UNION ALL SELECT 'LookupEntries', COUNT(*) FROM lep.LookupEntries
UNION ALL SELECT 'ProjectResources', COUNT(*) FROM lep.ProjectResources
UNION ALL SELECT 'SecondaryLaunchLeads', COUNT(*) FROM lep.SecondaryLaunchLeads
UNION ALL SELECT 'Dependencies', COUNT(*) FROM lep.Dependencies
UNION ALL SELECT 'Lookups', COUNT(*) FROM lep.Lookups
UNION ALL SELECT 'OptionalMilestones', COUNT(*) FROM lep.OptionalMilestones
UNION ALL SELECT 'TrackerAssociations', COUNT(*) FROM lep.TrackerAssociations
UNION ALL SELECT 'RegionCountryLeads', COUNT(*) FROM lep.RegionCountryLeads
UNION ALL SELECT 'FranchiseLeads', COUNT(*) FROM lep.FranchiseLeads
"""))

# COMMAND ----------
# MAGIC %md ## 2. Plan-type classification (T10 / §3.5)

# COMMAND ----------
display(q("""
SELECT plan_type, COUNT(*) AS total,
       SUM(CASE WHEN is_deleted=1 THEN 1 ELSE 0 END) AS deleted,
       SUM(CASE WHEN is_deleted=0 THEN 1 ELSE 0 END) AS live
FROM (
  SELECT CASE WHEN ISNULL(IsTemplate,0)=1 THEN 'TEMPLATE'
              WHEN ISNULL(IsMCEPlan,0)=1 THEN 'mce'
              WHEN ISNULL(IsGlobalPlan,0)=1 THEN 'global'
              ELSE 'local' END AS plan_type,
         ISNULL(IsDeleted,0) AS is_deleted
  FROM lep.Projects) x
GROUP BY plan_type ORDER BY total DESC
"""))

# COMMAND ----------
# raw flag combos (spot any plan flagged BOTH mce and global)
display(q("""
SELECT ISNULL(IsTemplate,0) AS IsTemplate, ISNULL(IsGlobalPlan,0) AS IsGlobalPlan,
       ISNULL(IsMCEPlan,0) AS IsMCEPlan, ISNULL(IsDeleted,0) AS IsDeleted, COUNT(*) AS n
FROM lep.Projects
GROUP BY ISNULL(IsTemplate,0),ISNULL(IsGlobalPlan,0),ISNULL(IsMCEPlan,0),ISNULL(IsDeleted,0)
ORDER BY n DESC
"""))

# COMMAND ----------
# MAGIC %md ## 3. Geography — country usage (RegionCountry -> LookupEntries)

# COMMAND ----------
# 3a. which lookup category (expect 'Takeda Region Country LT')
display(q("""
SELECT lk.Name AS category, COUNT(*) AS plans
FROM lep.Projects p
JOIN lep.LookupEntries le ON le.ID = p.RegionCountry
JOIN lep.Lookups lk ON lk.ID = le.Lookup
GROUP BY lk.Name ORDER BY plans DESC
"""))

# COMMAND ----------
# 3b. distinct country used by live plans (entry = 'Region.Country')
display(q("""
SELECT le.Name AS region_country, COUNT(*) AS plans
FROM lep.Projects p
LEFT JOIN lep.LookupEntries le ON le.ID = p.RegionCountry
WHERE ISNULL(p.IsTemplate,0)=0 AND ISNULL(p.IsDeleted,0)=0
GROUP BY le.Name ORDER BY plans DESC
"""))

# COMMAND ----------
# MAGIC %md ## 4. GEO-4 — dropped / alias countries in real data

# COMMAND ----------
display(q("""
SELECT le.Name AS country, COUNT(*) AS plans
FROM lep.Projects p
JOIN lep.LookupEntries le ON le.ID = p.RegionCountry
WHERE le.Name LIKE '%Hong Kong%' OR le.Name LIKE '%Haiti%' OR le.Name LIKE '%Grenada%'
   OR le.Name LIKE '%Tuvalu%' OR le.Name LIKE '%Greater China%'
   OR le.Name LIKE '%Greater Ecuador%' OR le.Name LIKE '%Carribean%'
GROUP BY le.Name ORDER BY plans DESC
"""))

# COMMAND ----------
# MAGIC %md ## 5. Plan hierarchy (parent/child, GEO-3 / 0.18)

# COMMAND ----------
display(q("""
SELECT
 SUM(CASE WHEN MCEPlanGUID IS NOT NULL AND GlobalPlanGuid IS NOT NULL THEN 1 ELSE 0 END) AS both_parents,
 SUM(CASE WHEN MCEPlanGUID IS NOT NULL AND GlobalPlanGuid IS NULL THEN 1 ELSE 0 END) AS mce_only,
 SUM(CASE WHEN MCEPlanGUID IS NULL AND GlobalPlanGuid IS NOT NULL THEN 1 ELSE 0 END) AS global_only,
 SUM(CASE WHEN MCEPlanGUID IS NULL AND GlobalPlanGuid IS NULL THEN 1 ELSE 0 END) AS no_parent
FROM lep.Projects
"""))

# COMMAND ----------
# orphan parent pointers (must be 0)
display(q("""
SELECT
 (SELECT COUNT(*) FROM lep.Projects c WHERE c.MCEPlanGUID IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM lep.Projects p WHERE p.ID=c.MCEPlanGUID)) AS orphan_mce,
 (SELECT COUNT(*) FROM lep.Projects c WHERE c.GlobalPlanGuid IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM lep.Projects p WHERE p.ID=c.GlobalPlanGuid)) AS orphan_global
"""))

# COMMAND ----------
# MAGIC %md ## 6. Lookups & entries per category

# COMMAND ----------
display(q("""
SELECT lk.Name AS category, COUNT(*) AS entries,
       SUM(CASE WHEN le.Active=1 THEN 1 ELSE 0 END) AS active
FROM lep.LookupEntries le JOIN lep.Lookups lk ON lk.ID=le.Lookup
GROUP BY lk.Name ORDER BY entries DESC
"""))

# COMMAND ----------
# all entries for one category (change the name)
display(q("""
SELECT le.Name AS entry, le.InternalName, le.Active, le.SortIndex
FROM lep.LookupEntries le JOIN lep.Lookups lk ON lk.ID=le.Lookup
WHERE lk.Name='Overall Launch Status LT' ORDER BY le.SortIndex, le.Name
"""))

# COMMAND ----------
# MAGIC %md ## 7. Crosswalk value distributions (status / function / role)

# COMMAND ----------
display(q("""
SELECT le.Name AS overall_launch_status, COUNT(*) AS plans
FROM lep.Projects p JOIN lep.LookupEntries le ON le.ID=p.OverallLaunchStatus
WHERE ISNULL(p.IsTemplate,0)=0 AND ISNULL(p.IsDeleted,0)=0
GROUP BY le.Name ORDER BY plans DESC
"""))

# COMMAND ----------
display(q("""
SELECT le.Name AS lep_status, COUNT(*) AS tasks
FROM lep.Tasks t JOIN lep.LookupEntries le ON le.ID=t.LepStatus
GROUP BY le.Name ORDER BY tasks DESC
"""))

# COMMAND ----------
display(q("""
SELECT le.Name AS task_function, COUNT(*) AS tasks
FROM lep.Tasks t JOIN lep.LookupEntries le ON le.ID=t.[Function]
GROUP BY le.Name ORDER BY tasks DESC
"""))

# COMMAND ----------
display(q("""
SELECT le.Name AS pr_role, COUNT(*) AS memberships
FROM lep.ProjectResources pr JOIN lep.LookupEntries le ON le.ID=pr.Role
GROUP BY le.Name ORDER BY memberships DESC
"""))

# COMMAND ----------
# MAGIC %md ## 8. Identity readiness (owners / assignees resolve)

# COMMAND ----------
display(q("""
SELECT COUNT(*) AS total_resources,
       COUNT(DISTINCT LOWER(ResourceEmailAddress)) AS distinct_emails,
       SUM(CASE WHEN Active=1 THEN 1 ELSE 0 END) AS active
FROM lep.Resources
"""))

# COMMAND ----------
display(q("""
SELECT COUNT(*) AS live_plans,
       SUM(CASE WHEN p.Owner IS NULL THEN 1 ELSE 0 END) AS owner_null,
       SUM(CASE WHEN p.Owner IS NOT NULL AND r.ID IS NULL THEN 1 ELSE 0 END) AS owner_unresolved
FROM lep.Projects p LEFT JOIN lep.Resources r ON r.ID=p.Owner
WHERE ISNULL(p.IsTemplate,0)=0 AND ISNULL(p.IsDeleted,0)=0
"""))

# COMMAND ----------
# MAGIC %md ## 9. Tracker feed (external systems writing the DB)

# COMMAND ----------
display(q("""
SELECT Source, COUNT(*) AS rows,
       MIN(LaunchDate) AS min_launch, MAX(LaunchDate) AS max_launch
FROM lep.Tracker GROUP BY Source ORDER BY rows DESC
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ### Notes
# MAGIC - `q()` pushes each SELECT to the server (keep them CTE-free — Spark wraps them as a subquery).
# MAGIC - For heavy Spark-side EDA, `tbl("Tasks")` pulls the whole table into a DataFrame.
# MAGIC - Country reconciliation vs the NEW taxonomy: join `le.Name`'s country part to
# MAGIC   Lakebase `lep.countries` (aliases: Greater Ecuador→Ecuador, Carribean→Caribbean;
# MAGIC   Hong Kong blocked). Verified logic lives in `scripts/migration/transforms.py`.
