"""Сквозной прогон стадий 1->2 и 2->3 против рабочей копии реального каталога."""
from __future__ import annotations
import shutil
import sqlite3
import sys
from . import config
from .catalog_repo import CatalogRepo
from . import stage_brief_to_catalog as s12
from . import stage_catalog_to_dag as s23
from . import storage

BRIEF = ("Подготовить junior backend-разработчика на Python для финтеха: уметь работать с "
         "БД и SQL, проектировать REST API, понимать очереди сообщений, владеть Docker и Git.")


def main(src_db: str, work_db: str, migration_sql: str) -> None:
    shutil.copyfile(src_db, work_db)            # не трогаем оригинал
    con = sqlite3.connect(work_db)
    con.row_factory = sqlite3.Row
    storage.apply_migration(con, migration_sql)
    repo = CatalogRepo(work_db)
    print(f"Каталог: {repo.canonical_count} канонических навыков, {repo.alias_count} синонимов")
    print(f"Режим: {'LIVE' if config.USE_LIVE else 'MOCK'}\n")

    # --- Стадия 1->2 ---
    spec, evidence, cands = s12.run(BRIEF, repo)
    print(f"[1->2] роль={spec['role']} | evidence={len(evidence)} | кандидатов={len(cands)}")
    for c in cands:
        tag = c.canonical_name or "—"
        print(f"   {c.name:42} {c.resolution:8} -> {tag[:38]:38} conf={c.confidence} [{c.decision}]")
    brief_id = storage.save_brief(con, BRIEF, spec)
    ev_idmap = storage.save_evidence(con, brief_id, evidence)
    storage.save_suggestions(con, brief_id, cands, ev_idmap)

    # --- Стадия 2->3 ---
    edges, DAG, rc, rt = s23.run(cands)
    print(f"\n[2->3] предложено рёбер={len(edges)} | разорвано циклов={len(rc)} | "
          f"убрано избыточных={len(rt)} | итоговый DAG: {DAG.number_of_edges()} рёбер, "
          f"ацикличен={__import__('networkx').is_directed_acyclic_graph(DAG)}")
    by = {c.tmp_id: c.name for c in cands}
    for u, v in rc:
        print(f"   цикл -> убрано слабейшее: {by[u]} -> {by[v]}")
    for u, v in rt:
        print(f"   избыточно -> убрано: {by[u]} -> {by[v]}")
    n_pre = storage.save_prerequisites(con, DAG, cands)

    # --- Чтение обратно из БД: подтверждение персистентности ---
    print("\n[persist] записано в рабочую БД:")
    for t in ["profile_brief", "evidence_source", "skill_suggestion", "skill_prerequisite"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"   {t:20} {n}")
    nopen = con.execute("SELECT COUNT(*) FROM review_queue WHERE status='open'").fetchone()[0]
    print(f"   review_queue(open)   {nopen}")
    con.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
