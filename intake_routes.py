"""Интейк брифа для Flask-вьюера Spravochnik.

Регистрируется как Blueprint. GET /intake — форма; POST /intake — прогон
стадий 1->2 и 2->3, запись в каталог, рендер всей информации для проверки.

Подключение в viewer/app.py:
    from viewer.intake_routes import intake_bp, init_intake
    init_intake(DB_PATH, MIGRATION_SQL)   # пути к skills_catalog.sqlite и sql/new_tables.sql
    app.register_blueprint(intake_bp)
    # и добавить в nav: {"href": "/intake", "label": "Бриф"}
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from flask import Blueprint, render_template, request

from pipeline import config
from pipeline.catalog_repo import CatalogRepo
from pipeline import stage_brief_to_catalog as s12
from pipeline import stage_catalog_to_dag as s23
from pipeline import storage

intake_bp = Blueprint("intake", __name__)
_DB_PATH = ""
_MIGRATION_SQL = ""

NAV = [
    {"href": "/competencies", "label": "Компетенции"},
    {"href": "/profiles", "label": "Профили"},
    {"href": "/reviews", "label": "Проверка"},
    {"href": "/intake", "label": "Бриф"},
]


def init_intake(db_path: str, migration_sql: str) -> None:
    global _DB_PATH, _MIGRATION_SQL
    _DB_PATH, _MIGRATION_SQL = db_path, migration_sql


def _summary(con: sqlite3.Connection) -> dict:
    def c(sql):
        try:
            return con.execute(sql).fetchone()[0]
        except sqlite3.Error:
            return "?"
    return {"counts": {
        "profiles": c("SELECT COUNT(*) FROM profile"),
        "competencies": c("SELECT COUNT(*) FROM competency"),
        "skills": c("SELECT COUNT(*) FROM skill"),
        "indicator_rows": c("SELECT COUNT(*) FROM indicator_row"),
        "open_reviews": c("SELECT COUNT(*) FROM review_queue WHERE status='open'"),
    }}


def _read_brief() -> str:
    text = (request.form.get("brief") or "").strip()
    f = request.files.get("brief_file")
    if f and f.filename:
        name = f.filename.lower()
        if name.endswith(".docx"):
            try:
                import docx  # python-docx
                doc = docx.Document(f)
                text = "\n".join(p.text for p in doc.paragraphs).strip()
            except Exception:
                text = text or "(не удалось прочитать .docx — установите python-docx)"
        else:
            text = f.read().decode("utf-8", errors="ignore").strip()
    return text


def _ctx(con: sqlite3.Connection, **extra) -> dict:
    base = {"title": "Бриф", "nav": NAV, "request_path": "/intake", "summary": _summary(con)}
    base.update(extra)
    return base


@intake_bp.route("/intake", methods=["GET"])
def intake_form():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return render_template("intake.html", **_ctx(con, brief="", result=None))
    finally:
        con.close()


@intake_bp.route("/intake", methods=["POST"])
def intake_run():
    brief = _read_brief()
    con = sqlite3.connect(_DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        if not brief:
            return render_template("intake.html", **_ctx(con, brief="", result=None))
        # схема (идемпотентно) — недостающие таблицы
        storage.apply_migration(con, _MIGRATION_SQL)
        repo = CatalogRepo(_DB_PATH)

        # --- Стадия 1->2 ---
        spec, evidence, cands = s12.run(brief, repo)
        brief_id = storage.save_brief(con, brief, spec)
        ev_idmap = storage.save_evidence(con, brief_id, evidence)
        storage.save_suggestions(con, brief_id, cands, ev_idmap)

        # --- Стадия 2->3 ---
        edges, DAG, rc, rt = s23.run(cands)
        import networkx as nx
        storage.save_prerequisites(con, DAG, cands)

        result = {
            "spec": spec,
            "evidence": [e.model_dump() for e in evidence],
            "candidates": [{
                "name": c.name, "group": c.group, "bloom": c.bloom,
                "tools": ", ".join(c.tools), "resolution": c.resolution,
                "canonical_name": c.canonical_name, "confidence": c.confidence,
                "council_agreement": c.council_agreement, "decision": c.decision,
                "reasons": ", ".join(c.reasons),
            } for c in cands],
            "dag": {"edges": DAG.number_of_edges(), "removed_cycle": len(rc),
                    "removed_transitive": len(rt), "acyclic": nx.is_directed_acyclic_graph(DAG)},
            "brief_id": brief_id,
            "persisted": {
                "evidence_source": con.execute("SELECT COUNT(*) FROM evidence_source WHERE brief_id=?", (brief_id,)).fetchone()[0],
                "skill_suggestion": con.execute("SELECT COUNT(*) FROM skill_suggestion WHERE brief_id=?", (brief_id,)).fetchone()[0],
                "skill_prerequisite": con.execute("SELECT COUNT(*) FROM skill_prerequisite").fetchone()[0],
                "review_open": con.execute("SELECT COUNT(*) FROM review_queue WHERE status='open'").fetchone()[0],
            },
        }
        return render_template("intake.html", **_ctx(con, brief=brief, result=result))
    finally:
        con.close()
