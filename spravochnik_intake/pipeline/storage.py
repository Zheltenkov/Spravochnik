"""Персистентность: применяет миграцию недостающих таблиц и пишет результаты."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from .models import Evidence, PrereqEdge, SkillCandidate

_REQUIRED_COLS = {
    "skill_suggestion": [
        ("entity_type", "TEXT NOT NULL DEFAULT 'skill'"),
        ("atomicity", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("parent_suggestion_id", "INTEGER"),
        ("atomize_rationale", "TEXT"),
    ],
}


def _existing_cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _supports_superseded(con: sqlite3.Connection) -> bool:
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='skill_suggestion'").fetchone()
    return bool(row and row[0] and "superseded" in row[0])


def apply_migration(con: sqlite3.Connection, sql_path: str) -> None:
    con.executescript(Path(sql_path).read_text(encoding="utf-8"))
    for table, cols in _REQUIRED_COLS.items():
        existing = _existing_cols(con, table)
        for name, decl in cols:
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
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


def save_suggestions(con: sqlite3.Connection, brief_id: int, cands: list[SkillCandidate], ev_idmap: dict[str, int]) -> dict[str, int]:
    tmp_to_db: dict[str, int] = {}
    allow_superseded = _supports_superseded(con)
    ordered = sorted(cands, key=lambda candidate: 0 if candidate.parent_tmp_id is None else 1)
    for c in ordered:
        parent_db_id = tmp_to_db.get(c.parent_tmp_id) if c.parent_tmp_id else None
        stored_decision = c.decision
        if stored_decision == "superseded" and not allow_superseded:
            stored_decision = "rejected"
        con.execute(
            """INSERT INTO skill_suggestion(brief_id, suggested_name, group_name, bloom, tools,
               resolution, canonical_skill_id, confidence, council_agreement, evidence_ids, decision,
               entity_type, atomicity, parent_suggestion_id, atomize_rationale)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                brief_id,
                c.name,
                c.group,
                max((i.bloom for i in c.indicators), default=None),
                json.dumps(c.tools, ensure_ascii=False),
                c.resolution,
                c.canonical_skill_id,
                c.confidence,
                c.council_agreement,
                json.dumps([ev_idmap.get(x) for x in c.evidence_ids]),
                stored_decision or "pending",
                c.entity_type,
                c.atomicity,
                parent_db_id,
                c.atomize_rationale,
            ),
        )
        tmp_to_db[c.tmp_id] = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        # спорное -> в существующую review_queue (переиспользуем механизм каталога)
        if c.decision == "needs_review":
            rq_entity_type = "competency" if c.entity_type == "competency_block" else "skill"
            primary_reason = c.reasons[0] if c.reasons else "needs_review"
            severity = "warning" if primary_reason in {"novel_skill", "council_split", "fuzzy_match_ambiguous", "low_confidence"} else "info"
            reasons_text = ", ".join(c.reasons) if c.reasons else "manual_review"
            details = (
                f"Интейк по брифу #{brief_id}: {c.entity_type} «{c.name}». "
                f"Атомарность: {c.atomicity}. "
                f"Резолв против каталога: {c.resolution or 'unknown'}, "
                f"уверенность {c.confidence:.2f}. "
                f"Причины проверки: {reasons_text}."
            )
            con.execute(
                """INSERT INTO review_queue(entity_type, entity_id, source_ref, reason_code, severity, details, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'open')""",
                (rq_entity_type, c.canonical_skill_id, f"brief:{brief_id}", primary_reason, severity, details),
            )
    con.commit()
    return tmp_to_db


def save_prerequisites(con: sqlite3.Connection, DAG, cands: list[SkillCandidate]) -> int:
    by_tid = {c.tmp_id: c for c in cands}
    n = 0
    for u, v in DAG.edges():
        cu, cv = by_tid[u], by_tid[v]
        edge = DAG[u][v].get("edge")
        con.execute(
            """INSERT INTO skill_prerequisite(src_skill_id, dst_skill_id, src_name, dst_name,
               relation_type, confidence, source, review_state)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                cu.canonical_skill_id,
                cv.canonical_skill_id,
                cu.name,
                cv.name,
                edge.relation_type if edge else "hard",
                DAG[u][v].get("conf"),
                edge.source if edge else "pipeline",
                "accepted" if (cu.canonical_skill_id and cv.canonical_skill_id and (edge is None or edge.decision == "accept")) else "needs_review",
            ),
        )
        n += 1
    con.commit()
    return n


def save_prerequisite_reviews(con: sqlite3.Connection, brief_id: int, edge_reviews: list[dict[str, object]]) -> int:
    count = 0
    for item in edge_reviews:
        con.execute(
            """
            INSERT INTO review_queue(entity_type, entity_id, source_ref, reason_code, severity, details, status)
            VALUES ('skill', NULL, ?, ?, ?, ?, 'open')
            """,
            (
                f"brief:{brief_id}",
                str(item.get("reason_code", "needs_review")),
                str(item.get("severity", "info")),
                json.dumps(
                    {
                        "review_kind": "prerequisite_edge",
                        "edge_key": item.get("edge_key"),
                        "edge_label": item.get("edge_label"),
                        "confidence": item.get("confidence"),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        count += 1
    con.commit()
    return count
