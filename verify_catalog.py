"""Проверка целостности и наполнения справочника (skills_catalog.sqlite)."""
import sqlite3, sys, json

DB = "artifacts/skills_catalog.sqlite"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

report = {}

# 1) Схема: список таблиц и представлений
tables = [r[0] for r in cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
views = [r[0] for r in cur.execute(
    "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
report["tables_count"] = len(tables)
report["views_count"] = len(views)

# 2) Наполнение ключевых таблиц
key_tables = ["profile","competency","skill","skill_alias","indicator_row",
              "indicator_level_cell","competency_skill","profile_competency",
              "ai_analysis_run","ai_analysis_suggestion","review_queue",
              "taxonomy_node","taxonomy_edge","source_workbook","source_block"]
counts = {}
for t in key_tables:
    if t in tables:
        counts[t] = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
report["row_counts"] = counts

# 3) Целостность FK: включаем и проверяем
cur.execute("PRAGMA foreign_keys=ON")
fk_violations = cur.execute("PRAGMA foreign_key_check").fetchall()
report["fk_violations"] = len(fk_violations)
report["fk_violation_sample"] = [dict(zip(["table","rowid","parent","fkid"], v)) for v in fk_violations[:5]]

# 4) integrity_check
integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
report["integrity_check"] = integrity

# 5) Осиротевшие связи: competency_skill -> skill
orphans = {}
if "competency_skill" in tables and "skill" in tables:
    orphans["competency_skill_no_skill"] = cur.execute(
        "SELECT COUNT(*) FROM competency_skill cs LEFT JOIN skill s ON cs.skill_id=s.id WHERE s.id IS NULL").fetchone()[0]
if "indicator_row" in tables and "skill" in tables:
    # indicator_row ссылается на competency_skill или skill — проверим по наличию столбца
    cols = [c[1] for c in cur.execute("PRAGMA table_info(indicator_row)")]
    orphans["indicator_row_cols"] = cols
report["orphans"] = orphans

# 6) Дубли канонических навыков (по нормализованному имени)
dup = cur.execute("""
    SELECT normalized_name, COUNT(*) c FROM skill
    WHERE normalized_name IS NOT NULL
    GROUP BY normalized_name HAVING c>1 ORDER BY c DESC LIMIT 5
""").fetchall() if "skill" in tables else []
report["duplicate_canonical_names"] = [dict(r) for r in dup]

# 7) Статусы навыков
if "skill" in tables:
    st = cur.execute("SELECT status, COUNT(*) c FROM skill GROUP BY status").fetchall()
    report["skill_status"] = {r["status"]: r["c"] for r in st}

# 8) Открытые ревью
if "review_queue" in tables:
    rq = cur.execute("SELECT status, COUNT(*) c FROM review_queue GROUP BY status").fetchall()
    report["review_queue_status"] = {r["status"]: r["c"] for r in rq}

# 9) Представления реально выполняются?
view_ok = {}
for v in views:
    try:
        cur.execute(f"SELECT * FROM {v} LIMIT 1").fetchall(); view_ok[v] = "ok"
    except Exception as e:
        view_ok[v] = f"FAIL: {e}"
report["views_executable"] = view_ok

print(json.dumps(report, ensure_ascii=False, indent=2))
con.close()
