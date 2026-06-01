"""Персистентность: применяет миграцию недостающих таблиц и пишет результаты."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from .models import Evidence, PrereqEdge, SkillCandidate


def apply_migration(con: sqlite3.Connection, sql_path: str) -> None:
    con.executescript(Path(sql_path).read_text(encoding="utf-8"))
    con.commit()


def save_brief(con: sqlite3.Connection, raw: str, spec: dict) -> int:
    cur = con.execute(
        "INSERT INTO profile_brief(raw_text, role, seniority, domain) VALUES (?,?,?,?)",
        (raw, spec.get("role"), spec.get("seniority"), spec.get("domain")))
    con.commit()
    return cur.lastrowid


def save_evidence(con: sqlite3.Connection, brief_id: int, evidence: list[Evidence]) -> dict[str, int]:
    idmap = {}
    for e in evidence:
        cur = con.execute(
            "INSERT INTO evidence_source(brief_id, claim, source_type, url, snippet, retrieved_at) VALUES (?,?,?,?,?,?)",
            (brief_id, e.claim, e.source_type, e.url, e.snippet, e.retrieved_at))
        idmap[e.id] = cur.lastrowid
    con.commit()
    return idmap


def save_suggestions(con: sqlite3.Connection, brief_id: int, cands: list[SkillCandidate], ev_idmap: dict[str, int]) -> None:
    for c in cands:
        con.execute(
            """INSERT INTO skill_suggestion(brief_id, suggested_name, group_name, bloom, tools,
               resolution, canonical_skill_id, confidence, council_agreement, evidence_ids, decision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (brief_id, c.name, c.group, max((i.bloom for i in c.indicators), default=None),
             json.dumps(c.tools, ensure_ascii=False),
             c.resolution, c.canonical_skill_id, c.confidence, c.council_agreement,
             json.dumps([ev_idmap.get(x) for x in c.evidence_ids]), c.decision))
        # спорное -> в существующую review_queue (переиспользуем механизм каталога)
        if c.decision == "needs_review":
            con.execute(
                """INSERT INTO review_queue(entity_type, entity_id, reason_code, severity, details, status)
                   VALUES ('skill', ?, ?, ?, ?, 'open')""",
                (c.canonical_skill_id, ",".join(c.reasons),
                 "warning" if ("novel_skill" in c.reasons or "council_split" in c.reasons) else "info",
                 json.dumps({"name": c.name, "resolution": c.resolution, "confidence": c.confidence}, ensure_ascii=False)))
    con.commit()


def save_prerequisites(con: sqlite3.Connection, DAG, cands: list[SkillCandidate]) -> int:
    by_tid = {c.tmp_id: c for c in cands}
    n = 0
    for u, v in DAG.edges():
        cu, cv = by_tid[u], by_tid[v]
        con.execute(
            """INSERT INTO skill_prerequisite(src_skill_id, dst_skill_id, src_name, dst_name,
               relation_type, confidence, source, review_state)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cu.canonical_skill_id, cv.canonical_skill_id, cu.name, cv.name,
             "hard", DAG[u][v].get("conf"), "pipeline",
             "accepted" if (cu.canonical_skill_id and cv.canonical_skill_id) else "needs_review"))
        n += 1
    con.commit()
    return n
