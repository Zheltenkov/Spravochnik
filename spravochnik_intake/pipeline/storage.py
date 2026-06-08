"""Персистентность: применяет миграцию недостающих таблиц и пишет результаты."""
from __future__ import annotations
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from . import competency_catalog, config
from .models import Evidence, SkillCandidate

_REQUIRED_COLS = {
    "skill_suggestion": [
        ("coverage_area", "TEXT"),
        ("source_name", "TEXT"),
        ("indicators_json", "TEXT"),
        ("entity_type", "TEXT NOT NULL DEFAULT 'skill'"),
        ("atomicity", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("parent_suggestion_id", "INTEGER"),
        ("atomize_rationale", "TEXT"),
        ("match_score", "REAL"),
        ("nearest_skill_id", "INTEGER"),
        ("nearest_name", "TEXT"),
        ("nearest_group", "TEXT"),
    ],
    "skill_prerequisite": [
        ("brief_id", "INTEGER"),
        ("src_suggestion_id", "INTEGER"),
        ("dst_suggestion_id", "INTEGER"),
    ],
    "curriculum_plan_row": [
        ("outcomes_know", "TEXT"),
        ("outcomes_can", "TEXT"),
        ("outcomes_skills", "TEXT"),
        ("materials", "TEXT"),
        ("validation_criteria", "TEXT"),
        ("completion_percent", "REAL"),
        ("p2p_checks", "INTEGER"),
        ("weighted_skills", "TEXT"),
    ],
}

_REVIEW_QUEUE_ENTITY_TYPE_MAP = {
    "skill": "skill",
    "competency_block": "block",
    "curriculum_section": "block",
}


def _existing_cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _supports_superseded(con: sqlite3.Connection) -> bool:
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='skill_suggestion'").fetchone()
    return bool(row and row[0] and "superseded" in row[0])


def _quoted_columns(columns: list[str]) -> str:
    return ", ".join(f'"{column}"' for column in columns)


def _copy_common_columns(con: sqlite3.Connection, source_table: str, target_table: str) -> None:
    source_cols = _existing_cols(con, source_table)
    target_cols = _existing_cols(con, target_table)
    common = [column for column in target_cols if column in source_cols]
    if not common:
        return
    column_sql = _quoted_columns(common)
    con.execute(f'INSERT INTO "{target_table}"({column_sql}) SELECT {column_sql} FROM "{source_table}"')


def _ensure_curriculum_plan_accepts_invalid(con: sqlite3.Connection, sql_path: str) -> None:
    """Rebuild old curriculum_plan tables whose CHECK does not allow invalid.

    SQLite cannot alter CHECK constraints in place. The rebuild preserves parent
    and row records, then recreates indexes through the idempotent schema.
    """

    if not _table_exists(con, "curriculum_plan"):
        return
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='curriculum_plan'").fetchone()
    table_sql = str(row[0] or "") if row else ""
    if "invalid" in table_sql:
        return

    child_exists = _table_exists(con, "curriculum_plan_row")
    fk_state = int(con.execute("PRAGMA foreign_keys").fetchone()[0] or 0)
    con.commit()
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.execute("DROP TABLE IF EXISTS _curriculum_plan_row_backup")
        if child_exists:
            con.execute("CREATE TEMP TABLE _curriculum_plan_row_backup AS SELECT * FROM curriculum_plan_row")
            con.execute("DROP TABLE curriculum_plan_row")
        con.execute("DROP INDEX IF EXISTS idx_curriculum_plan_brief_policy")
        con.execute("ALTER TABLE curriculum_plan RENAME TO _curriculum_plan_old")
        con.executescript(Path(sql_path).read_text(encoding="utf-8"))
        _copy_common_columns(con, "_curriculum_plan_old", "curriculum_plan")
        con.execute("DROP TABLE _curriculum_plan_old")
        if child_exists:
            _copy_common_columns(con, "_curriculum_plan_row_backup", "curriculum_plan_row")
            con.execute("DROP TABLE _curriculum_plan_row_backup")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_curriculum_plan_brief_policy "
            "ON curriculum_plan(brief_id, source_policy)"
        )
        con.commit()
    finally:
        con.execute(f"PRAGMA foreign_keys = {fk_state}")


def _review_queue_entity_type(candidate: SkillCandidate) -> str:
    return _REVIEW_QUEUE_ENTITY_TYPE_MAP.get(candidate.entity_type, "block")


def apply_migration(con: sqlite3.Connection, sql_path: str) -> None:
    con.executescript(Path(sql_path).read_text(encoding="utf-8"))
    _ensure_curriculum_plan_accepts_invalid(con, sql_path)
    for table, cols in _REQUIRED_COLS.items():
        existing = _existing_cols(con, table)
        for name, decl in cols:
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_suggestion_brief_decision ON skill_suggestion(brief_id, entity_type, atomicity, decision)"
    )
    if "brief_id" in _existing_cols(con, "skill_prerequisite"):
        con.execute("CREATE INDEX IF NOT EXISTS idx_skill_prerequisite_brief ON skill_prerequisite(brief_id)")
    if _existing_cols(con, "review_queue"):
        con.execute("CREATE INDEX IF NOT EXISTS idx_review_queue_source_ref ON review_queue(source_ref, status)")
    if _table_exists(con, "skill_set_item"):
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_set_item_unique ON skill_set_item(skill_set_id, skill_id, role, COALESCE(plan_row_id, 0))"
        )
    con.commit()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_catalog_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower().strip()
    normalized = re.sub(r"[^0-9a-zа-яё+ ]", " ", normalized)
    return re.sub(r"\s+", " ", normalized)


def load_curriculum_artifact_templates(con: sqlite3.Connection, active_only: bool = True) -> list[dict[str, object]]:
    """Load active methodologist-managed artifact templates for the UP planner."""
    if not _table_exists(con, "curriculum_artifact_template"):
        return []
    status_filter = "WHERE status = 'active'" if active_only else ""
    rows = con.execute(
        f"""
        SELECT id, code, title, artifact_family, artifact_description,
               project_name_pattern, materials_pattern, storytelling_pattern,
               validation_criteria, priority, status, source
        FROM curriculum_artifact_template
        {status_filter}
        ORDER BY priority ASC, id ASC
        """
    ).fetchall()
    templates: list[dict[str, object]] = []
    for row in rows:
        template = dict(row)
        template_id = int(template["id"])
        scopes: list[dict[str, object]] = []
        if _table_exists(con, "curriculum_artifact_template_scope"):
            scope_rows = con.execute(
                """
                SELECT scope_type, scope_id, scope_name, normalized_scope_name, weight
                FROM curriculum_artifact_template_scope
                WHERE template_id = ?
                ORDER BY weight DESC, id ASC
                """,
                (template_id,),
            ).fetchall()
            for scope_row in scope_rows:
                scope = dict(scope_row)
                if not scope.get("normalized_scope_name") and scope.get("scope_name"):
                    scope["normalized_scope_name"] = _normalize_catalog_key(str(scope["scope_name"]))
                scopes.append(scope)
        template["scopes"] = scopes
        templates.append(template)
    return templates


def upsert_curriculum_artifact_template(
    con: sqlite3.Connection,
    *,
    code: str,
    title: str,
    artifact_family: str,
    artifact_description: str,
    project_name_pattern: str = "",
    materials_pattern: str = "",
    storytelling_pattern: str = "",
    validation_criteria: str = "",
    priority: int = 100,
    status: str = "active",
    source: str = "manual",
    scopes: list[dict[str, object]] | None = None,
) -> int:
    """Create or update a methodology-owned artifact template."""
    normalized_code = _slug_catalog_key(code or title)
    con.execute(
        """
        INSERT INTO curriculum_artifact_template(
            code, title, artifact_family, artifact_description, project_name_pattern,
            materials_pattern, storytelling_pattern, validation_criteria, priority,
            status, source, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            title = excluded.title,
            artifact_family = excluded.artifact_family,
            artifact_description = excluded.artifact_description,
            project_name_pattern = excluded.project_name_pattern,
            materials_pattern = excluded.materials_pattern,
            storytelling_pattern = excluded.storytelling_pattern,
            validation_criteria = excluded.validation_criteria,
            priority = excluded.priority,
            status = excluded.status,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (
            normalized_code,
            title,
            artifact_family,
            artifact_description,
            project_name_pattern,
            materials_pattern,
            storytelling_pattern,
            validation_criteria,
            priority,
            status,
            source,
            _utc_now_iso(),
        ),
    )
    row = con.execute("SELECT id FROM curriculum_artifact_template WHERE code = ?", (normalized_code,)).fetchone()
    template_id = int(row["id"])
    con.execute("DELETE FROM curriculum_artifact_template_scope WHERE template_id = ?", (template_id,))
    for scope in scopes or []:
        scope_type = str(scope.get("scope_type") or "coverage_area").strip()
        scope_name = str(scope.get("scope_name") or "").strip()
        normalized_scope_name = str(scope.get("normalized_scope_name") or "").strip() or _normalize_catalog_key(scope_name)
        con.execute(
            """
            INSERT INTO curriculum_artifact_template_scope(
                template_id, scope_type, scope_id, scope_name, normalized_scope_name, weight
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                template_id,
                scope_type,
                scope.get("scope_id"),
                scope_name or None,
                normalized_scope_name or None,
                float(scope.get("weight", 1.0) or 1.0),
            ),
        )
    con.commit()
    return template_id


def _json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def load_curriculum_artifact_template_proposals(
    con: sqlite3.Connection,
    brief_id: int,
    status: str | None = None,
) -> list[dict[str, object]]:
    """Load human-reviewable UP template proposals for a brief."""
    if not _table_exists(con, "curriculum_artifact_template_proposal"):
        return []
    params: list[object] = [brief_id]
    status_filter = ""
    if status:
        status_filter = "AND status = ?"
        params.append(status)
    rows = con.execute(
        f"""
        SELECT id, brief_id, plan_id, status, code, title, artifact_family,
               scope_type, scope_names_json, artifact_description,
               project_name_pattern, materials_pattern, storytelling_pattern,
               validation_criteria, covered_skill_ids_json, covered_skill_names_json,
               rationale, confidence, source, accepted_template_id, created_at, updated_at
        FROM curriculum_artifact_template_proposal
        WHERE brief_id = ?
          {status_filter}
        ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                 id ASC
        """,
        tuple(params),
    ).fetchall()
    proposals: list[dict[str, object]] = []
    for row in rows:
        proposal = dict(row)
        proposal["scope_names"] = [str(item) for item in _json_list(proposal.get("scope_names_json")) if str(item).strip()]
        proposal["covered_skill_ids"] = [int(item) for item in _json_list(proposal.get("covered_skill_ids_json")) if str(item).strip().isdigit()]
        proposal["covered_skill_names"] = [str(item) for item in _json_list(proposal.get("covered_skill_names_json")) if str(item).strip()]
        proposals.append(proposal)
    return proposals


def _artifact_family_for_scope(scope_name: str, skill_names: list[str]) -> str:
    scope_text = _normalize_catalog_key(scope_name)
    full_text = _normalize_catalog_key(" ".join([scope_name, *skill_names]))
    if any(token in scope_text for token in ["инженер", "репозитор", "ci", "тест", "инфраструкт", "развер", "наблюдаем", "резерв", "инцидент"]):
        return "configuration"
    if any(token in scope_text for token in ["архитект", "mvp", "приорит"]):
        return "design"
    if any(token in scope_text for token in ["workflow", "автоматизац", "рабочих процессов"]):
        return "production"
    if any(token in scope_text for token in ["интерв", "исслед", "гипотез", "эксперимент", "инсайт", "выявление", "контроль качества"]):
        return "analysis"
    if any(token in scope_text for token in ["маркет", "продаж", "монет", "правов", "финансов", "стратег", "okr", "позиционир", "ценност"]):
        return "document"
    if "поддерж" in scope_text or "обрат" in scope_text:
        return "practice"
    if any(token in full_text for token in ["ci", "репозитор", "тест", "развер", "монитор", "резерв", "инцидент"]):
        return "configuration"
    if any(token in full_text for token in ["интерв", "исслед", "гипотез", "эксперимент", "инсайт", "качество", "риск"]):
        return "analysis"
    return "practice"


def _compact_scope_title(scope_name: str) -> str:
    cleaned = re.sub(r"\s+", " ", scope_name).strip(" .;:")
    if not cleaned:
        return "проектный артефакт"
    parts = re.split(r"[:;,]", cleaned)
    title = parts[0].strip() if parts else cleaned
    words = title.split()
    return " ".join(words[:7]) if len(words) > 7 else title


def _proposal_title(scope_name: str, skill_names: list[str], family: str) -> str:
    family_titles = {
        "analysis": "Аналитический артефакт",
        "document": "Документальный артефакт",
        "configuration": "Конфигурационный артефакт",
        "design": "Проектный артефакт",
        "production": "Рабочий артефакт",
        "practice": "Практический артефакт",
    }
    return f"{family_titles.get(family, 'Проектный артефакт')}: {_compact_scope_title(scope_name)}"


def _proposal_payload(scope_name: str, skill_rows: list[sqlite3.Row]) -> dict[str, object]:
    skill_names = [
        str(row["canonical_name"] or row["suggested_name"]).strip()
        for row in skill_rows
        if str(row["canonical_name"] or row["suggested_name"] or "").strip()
    ]
    skill_ids = [int(row["id"]) for row in skill_rows]
    family = _artifact_family_for_scope(scope_name, skill_names)
    title = _proposal_title(scope_name, skill_names, family)
    skills_placeholder = "{skills}"
    theme_placeholder = "{theme}"
    artifact_description = (
        f"{title.lower()} по теме «{theme_placeholder}»: студент предъявляет проверяемый результат, "
        f"в котором явно применены навыки {skills_placeholder}."
    )
    return {
        "code": f"proposal-{_slug_catalog_key(scope_name)}",
        "title": title,
        "artifact_family": family,
        "scope_type": "coverage_area",
        "scope_names": [scope_name],
        "artifact_description": artifact_description,
        "project_name_pattern": title,
        "materials_pattern": (
            f"Бриф продукта, шаблон артефакта «{title}», список навыков: {skills_placeholder}, "
            "рабочие документы/таблицы, LLM для черновиков и проверки полноты."
        ),
        "storytelling_pattern": (
            "Участник действует как основатель продукта: собирает рабочий артефакт для реального запуска, "
            "обосновывает решения данными и показывает, как применил навыки."
        ),
        "validation_criteria": (
            f"Артефакт «{title}» предъявлен; покрыты навыки {skills_placeholder}; есть проверяемые выводы, "
            "обоснования решений и следующий шаг для продукта."
        ),
        "covered_skill_ids": skill_ids,
        "covered_skill_names": skill_names,
        "rationale": (
            f"В брифе есть область «{scope_name}» и {len(skill_names)} accepted skills. "
            "Шаблон нужен, чтобы заменить generic-проект на проверяемый артефакт."
        ),
        "confidence": 0.78 if len(skill_names) >= 2 else 0.68,
        "source": "deterministic_fallback",
    }


def _brief_context_for_consilium(con: sqlite3.Connection, brief_id: int) -> dict[str, object]:
    row = con.execute(
        """
        SELECT id, raw_text, role, seniority, domain, created_at
        FROM profile_brief
        WHERE id = ?
        """,
        (brief_id,),
    ).fetchone()
    return dict(row) if row else {"id": brief_id, "raw_text": "", "role": "", "seniority": "", "domain": ""}


def _scope_groups_for_consilium(grouped: dict[str, list[sqlite3.Row]]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    for scope_name, skill_rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        skills: list[dict[str, object]] = []
        for row in skill_rows:
            skill_name = str(row["canonical_name"] or row["suggested_name"] or "").strip()
            skills.append(
                {
                    "id": int(row["id"]),
                    "name": skill_name,
                    "group_name": str(row["group_name"] or "").strip(),
                    "bloom": str(row["bloom"] or "").strip(),
                    "tools": str(row["tools"] or "").strip(),
                    "source_name": str(row["source_name"] or "").strip(),
                }
            )
        groups.append(
            {
                "scope_name": scope_name,
                "skill_count": len(skills),
                "skills": skills,
            }
        )
    return groups


def generate_curriculum_artifact_template_proposals(
    con: sqlite3.Connection,
    *,
    brief_id: int,
    plan_id: int | None = None,
    max_proposals: int = 10,
) -> list[dict[str, object]]:
    """Generate idempotent template proposals from accepted skills.

    The preferred path is LLM consilium with strict prompts and validation.
    Deterministic generation remains a safe fallback for offline/dev runs.
    """
    if not _table_exists(con, "curriculum_artifact_template_proposal"):
        return []
    rows = con.execute(
        """
        SELECT ss.id, ss.suggested_name, ss.group_name, ss.coverage_area,
               ss.bloom, ss.tools, ss.source_name, ss.indicators_json,
               ss.canonical_skill_id, s.canonical_name
        FROM skill_suggestion ss
        LEFT JOIN skill s ON s.id = ss.canonical_skill_id
        WHERE ss.brief_id = ?
          AND ss.entity_type = 'skill'
          AND ss.atomicity = 'atomic'
          AND ss.decision = 'accepted'
        ORDER BY ss.id
        """,
        (brief_id,),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        scope_name = str(row["coverage_area"] or row["group_name"] or "Общее").strip()
        if not scope_name:
            continue
        grouped.setdefault(scope_name, []).append(row)

    fallback_payloads = [
        _proposal_payload(scope_name, skill_rows)
        for scope_name, skill_rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))[:max_proposals]
    ]
    payloads = fallback_payloads
    if config.USE_UP_TEMPLATE_CONSILIUM and grouped:
        try:
            from . import up_template_consilium

            payloads = up_template_consilium.propose(
                brief=_brief_context_for_consilium(con, brief_id),
                scope_groups=_scope_groups_for_consilium(grouped),
                max_proposals=max_proposals,
            )
        except Exception as exc:
            fallback_reason = f"LLM-консилиум недоступен или вернул некорректный JSON: {exc}"
            payloads = []
            for payload in fallback_payloads:
                payload = dict(payload)
                payload["source"] = "deterministic_fallback_after_llm_error"
                payload["rationale"] = f"{payload.get('rationale', '')} {fallback_reason}".strip()
                payloads.append(payload)

    proposals: list[dict[str, object]] = []
    con.execute(
        """
        DELETE FROM curriculum_artifact_template_proposal
        WHERE brief_id = ?
          AND status = 'open'
        """,
        (brief_id,),
    )
    for payload in payloads:
        code = str(payload["code"])
        existing = con.execute(
            """
            SELECT id, status
            FROM curriculum_artifact_template_proposal
            WHERE brief_id = ? AND code = ?
            """,
            (brief_id, code),
        ).fetchone()
        if existing and str(existing["status"]) != "open":
            continue
        values = (
            brief_id,
            plan_id,
            "open",
            code,
            payload["title"],
            payload["artifact_family"],
            payload["scope_type"],
            json.dumps(payload["scope_names"], ensure_ascii=False),
            payload["artifact_description"],
            payload["project_name_pattern"],
            payload["materials_pattern"],
            payload["storytelling_pattern"],
            payload["validation_criteria"],
            json.dumps(payload["covered_skill_ids"], ensure_ascii=False),
            json.dumps(payload["covered_skill_names"], ensure_ascii=False),
            payload["rationale"],
            float(payload["confidence"]),
            str(payload.get("source") or "deterministic_fallback"),
            _utc_now_iso(),
        )
        con.execute(
            """
            INSERT INTO curriculum_artifact_template_proposal(
                brief_id, plan_id, status, code, title, artifact_family, scope_type,
                scope_names_json, artifact_description, project_name_pattern,
                materials_pattern, storytelling_pattern, validation_criteria,
                covered_skill_ids_json, covered_skill_names_json, rationale,
                confidence, source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(brief_id, code) DO UPDATE SET
                plan_id = COALESCE(excluded.plan_id, plan_id),
                title = excluded.title,
                artifact_family = excluded.artifact_family,
                scope_type = excluded.scope_type,
                scope_names_json = excluded.scope_names_json,
                artifact_description = excluded.artifact_description,
                project_name_pattern = excluded.project_name_pattern,
                materials_pattern = excluded.materials_pattern,
                storytelling_pattern = excluded.storytelling_pattern,
                validation_criteria = excluded.validation_criteria,
                covered_skill_ids_json = excluded.covered_skill_ids_json,
                covered_skill_names_json = excluded.covered_skill_names_json,
                rationale = excluded.rationale,
                confidence = excluded.confidence,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            values,
        )
        proposals.append(payload)
    con.commit()
    return load_curriculum_artifact_template_proposals(con, brief_id)


def update_curriculum_artifact_template_proposal(
    con: sqlite3.Connection,
    proposal_id: int,
    *,
    title: str,
    artifact_family: str,
    scope_type: str,
    scope_names: list[str],
    artifact_description: str,
    project_name_pattern: str,
    materials_pattern: str,
    storytelling_pattern: str,
    validation_criteria: str,
    rationale: str = "",
    confidence: float | None = None,
) -> None:
    con.execute(
        """
        UPDATE curriculum_artifact_template_proposal
        SET title = ?,
            artifact_family = ?,
            scope_type = ?,
            scope_names_json = ?,
            artifact_description = ?,
            project_name_pattern = ?,
            materials_pattern = ?,
            storytelling_pattern = ?,
            validation_criteria = ?,
            rationale = COALESCE(NULLIF(?, ''), rationale),
            confidence = COALESCE(?, confidence),
            updated_at = ?
        WHERE id = ?
        """,
        (
            title.strip() or "Шаблон УП",
            artifact_family,
            scope_type,
            json.dumps([name.strip() for name in scope_names if name.strip()], ensure_ascii=False),
            artifact_description.strip(),
            project_name_pattern.strip(),
            materials_pattern.strip(),
            storytelling_pattern.strip(),
            validation_criteria.strip(),
            rationale.strip(),
            confidence,
            _utc_now_iso(),
            proposal_id,
        ),
    )
    con.commit()


def accept_curriculum_artifact_template_proposal(con: sqlite3.Connection, proposal_id: int) -> dict[str, object]:
    proposal = con.execute(
        """
        SELECT *
        FROM curriculum_artifact_template_proposal
        WHERE id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if not proposal:
        return {"status": "missing", "proposal_id": proposal_id}
    scope_names = [str(item) for item in _json_list(proposal["scope_names_json"]) if str(item).strip()]
    template_id = upsert_curriculum_artifact_template(
        con,
        code=str(proposal["code"]),
        title=str(proposal["title"]),
        artifact_family=str(proposal["artifact_family"]),
        artifact_description=str(proposal["artifact_description"]),
        project_name_pattern=str(proposal["project_name_pattern"] or ""),
        materials_pattern=str(proposal["materials_pattern"] or ""),
        storytelling_pattern=str(proposal["storytelling_pattern"] or ""),
        validation_criteria=str(proposal["validation_criteria"] or ""),
        priority=80,
        status="active",
        source="proposal_accept",
        scopes=[
            {
                "scope_type": str(proposal["scope_type"] or "coverage_area"),
                "scope_name": scope_name,
                "weight": 1.0,
            }
            for scope_name in (scope_names or ["*"])
        ],
    )
    con.execute(
        """
        UPDATE curriculum_artifact_template_proposal
        SET status = 'accepted',
            accepted_template_id = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (template_id, _utc_now_iso(), proposal_id),
    )
    con.commit()
    return {"status": "accepted", "proposal_id": proposal_id, "template_id": template_id}


def reject_curriculum_artifact_template_proposal(con: sqlite3.Connection, proposal_id: int) -> None:
    con.execute(
        """
        UPDATE curriculum_artifact_template_proposal
        SET status = 'rejected',
            updated_at = ?
        WHERE id = ?
        """,
        (_utc_now_iso(), proposal_id),
    )
    con.commit()


def _slug_catalog_key(value: str) -> str:
    normalized = _normalize_catalog_key(value)
    slug = "-".join(part for part in normalized.split() if part)
    return slug or "item"


def _ensure_skill_group(con: sqlite3.Connection, group_name: str | None) -> int | None:
    if not _table_exists(con, "skill_group"):
        return None
    name = (group_name or "Прочие навыки").strip() or "Прочие навыки"
    code = f"group-{_slug_catalog_key(name)}"
    row = con.execute(
        "SELECT id FROM skill_group WHERE code = ? OR name = ? ORDER BY id LIMIT 1",
        (code, name),
    ).fetchone()
    if row:
        return int(row["id"])
    max_order = con.execute("SELECT COALESCE(MAX(sort_order), 0) FROM skill_group").fetchone()[0] or 0
    cursor = con.execute(
        """
        INSERT INTO skill_group(code, name, sort_order, status, source, updated_at)
        VALUES (?, ?, ?, 'active', 'derived', ?)
        """,
        (code, name, int(max_order) + 10, _utc_now_iso()),
    )
    return int(cursor.lastrowid)


def _load_skill_suggestion_row(con: sqlite3.Connection, suggestion_id: int) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT id, brief_id, suggested_name, source_name, group_name, coverage_area, resolution,
               canonical_skill_id, nearest_skill_id, nearest_name, nearest_group,
               decision, entity_type, atomicity, indicators_json
        FROM skill_suggestion
        WHERE id = ?
        """,
        (suggestion_id,),
    ).fetchone()


def _find_skill_by_id(con: sqlite3.Connection, skill_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, normalized_name, canonical_name, skill_type, status FROM skill WHERE id = ?",
        (skill_id,),
    ).fetchone()


def _find_skill_by_normalized_name(con: sqlite3.Connection, normalized_name: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, normalized_name, canonical_name, skill_type, status FROM skill WHERE normalized_name = ?",
        (normalized_name,),
    ).fetchone()


def _ensure_skill_alias(con: sqlite3.Connection, skill_id: int, alias: str, source: str) -> bool:
    if not alias or not alias.strip():
        return False
    normalized_alias = _normalize_catalog_key(alias)
    if not normalized_alias:
        return False
    exists = con.execute(
        "SELECT 1 FROM skill_alias WHERE skill_id = ? AND normalized_alias = ?",
        (skill_id, normalized_alias),
    ).fetchone()
    if exists:
        return False
    con.execute(
        """
        INSERT INTO skill_alias(skill_id, alias, normalized_alias, source)
        VALUES (?, ?, ?, ?)
        """,
        (skill_id, alias.strip(), normalized_alias, source),
    )
    return True


def _existing_promotion(con: sqlite3.Connection, suggestion_id: int) -> sqlite3.Row | None:
    if "skill_promotion_log" not in {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return None
    return con.execute(
        """
        SELECT id, suggestion_id, skill_id, alias, normalized_alias, created_skill, created_alias, status
        FROM skill_promotion_log
        WHERE suggestion_id = ?
        """,
        (suggestion_id,),
    ).fetchone()


def _skillset_code(*parts: object) -> str:
    return "skillset-" + "-".join(_slug_catalog_key(str(part)) for part in parts if str(part or "").strip())


def _accepted_atomic_skill_rows(con: sqlite3.Connection, brief_id: int) -> list[sqlite3.Row]:
    if not _table_exists(con, "skill_suggestion"):
        return []
    return con.execute(
        """
        SELECT
            ss.id AS suggestion_id,
            ss.canonical_skill_id AS skill_id,
            ss.suggested_name,
            ss.group_name,
            ss.coverage_area,
            ss.confidence,
            s.canonical_name
        FROM skill_suggestion ss
        JOIN skill s ON s.id = ss.canonical_skill_id
        WHERE ss.brief_id = ?
          AND ss.entity_type = 'skill'
          AND ss.atomicity = 'atomic'
          AND ss.decision = 'accepted'
          AND ss.canonical_skill_id IS NOT NULL
        ORDER BY COALESCE(ss.coverage_area, ss.group_name, ''), ss.id
        """,
        (brief_id,),
    ).fetchall()


def upsert_skill_set(
    con: sqlite3.Connection,
    *,
    code: str,
    title: str,
    source_type: str,
    source_id: int | None = None,
    source_ref: str = "",
    description: str = "",
    status: str = "active",
    metadata: dict[str, object] | None = None,
) -> int | None:
    """Create or update a reusable skill set without touching catalog taxonomy."""
    if not _table_exists(con, "skill_set"):
        return None
    normalized_code = _skillset_code(code)
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    con.execute(
        """
        INSERT INTO skill_set(
            code, title, description, source_type, source_id, source_ref,
            status, metadata_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            source_type = excluded.source_type,
            source_id = excluded.source_id,
            source_ref = excluded.source_ref,
            status = excluded.status,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            normalized_code,
            title.strip(),
            description.strip(),
            source_type,
            source_id,
            source_ref.strip(),
            status,
            metadata_json,
            _utc_now_iso(),
        ),
    )
    row = con.execute("SELECT id FROM skill_set WHERE code = ?", (normalized_code,)).fetchone()
    return int(row["id"]) if row else None


def replace_skill_set_items(
    con: sqlite3.Connection,
    skill_set_id: int,
    items: list[dict[str, object]],
) -> int:
    """Rewrite skill-set membership idempotently."""
    if not _table_exists(con, "skill_set_item"):
        return 0
    con.execute("DELETE FROM skill_set_item WHERE skill_set_id = ?", (skill_set_id,))
    inserted = 0
    for index, item in enumerate(items, start=1):
        skill_id = int(item.get("skill_id") or 0)
        if not skill_id:
            continue
        con.execute(
            """
            INSERT OR IGNORE INTO skill_set_item(
                skill_set_id, skill_id, suggestion_id, plan_row_id, role,
                weight, sort_order, rationale
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_set_id,
                skill_id,
                item.get("suggestion_id"),
                item.get("plan_row_id"),
                str(item.get("role") or "target"),
                float(item.get("weight") or 1.0),
                int(item.get("sort_order") or index),
                str(item.get("rationale") or ""),
            ),
        )
        inserted += 1
    return inserted


def sync_brief_skill_set(con: sqlite3.Connection, brief_id: int) -> dict[str, object]:
    """Persist accepted atomic skills as a reusable skill set for the brief."""
    rows = _accepted_atomic_skill_rows(con, brief_id)
    if not _table_exists(con, "skill_set"):
        return {"status": "skipped", "brief_id": brief_id, "item_count": 0}
    if not rows:
        code = _skillset_code(f"brief-{brief_id}-accepted")
        existing = con.execute("SELECT id FROM skill_set WHERE code = ?", (code,)).fetchone()
        if existing:
            skill_set_id = int(existing["id"])
            if _table_exists(con, "skill_set_item"):
                con.execute("DELETE FROM skill_set_item WHERE skill_set_id = ?", (skill_set_id,))
            con.execute(
                "UPDATE skill_set SET status = 'archived', updated_at = ? WHERE id = ?",
                (_utc_now_iso(), skill_set_id),
            )
        return {"status": "archived_empty", "brief_id": brief_id, "item_count": 0}
    skill_set_id = upsert_skill_set(
        con,
        code=f"brief-{brief_id}-accepted",
        title=f"Набор skills по брифу #{brief_id}",
        source_type="brief",
        source_id=brief_id,
        source_ref=f"brief:{brief_id}",
        description="Принятые методологом атомарные skills, используемые для DAG и УП.",
        metadata={
            "brief_id": brief_id,
            "item_count": len(rows),
            "coverage_areas": sorted({str(row["coverage_area"] or row["group_name"] or "").strip() for row in rows if str(row["coverage_area"] or row["group_name"] or "").strip()}),
        },
    )
    if skill_set_id is None:
        return {"status": "skipped", "brief_id": brief_id, "item_count": 0}
    items = [
        {
            "skill_id": int(row["skill_id"]),
            "suggestion_id": int(row["suggestion_id"]),
            "role": "target",
            "weight": 1.0,
            "sort_order": index,
            "rationale": f"accepted_atomic:{row['suggestion_id']}",
        }
        for index, row in enumerate(rows, start=1)
    ]
    item_count = replace_skill_set_items(con, skill_set_id, items)
    return {"status": "synced", "brief_id": brief_id, "skill_set_id": skill_set_id, "item_count": item_count}


def sync_curriculum_plan_skill_set(
    con: sqlite3.Connection,
    *,
    brief_id: int,
    plan_id: int,
    plan_payload: dict[str, object],
) -> dict[str, object]:
    """Persist the skill set used by a curriculum plan without changing plan rows."""
    rows = _accepted_atomic_skill_rows(con, brief_id)
    if not rows or not _table_exists(con, "skill_set"):
        return {"status": "skipped", "plan_id": plan_id, "item_count": 0}
    report = plan_payload.get("report") if isinstance(plan_payload.get("report"), dict) else {}
    quality_metrics = report.get("quality_metrics") if isinstance(report.get("quality_metrics"), dict) else {}
    skill_set_id = upsert_skill_set(
        con,
        code=f"curriculum-plan-{plan_id}-skills",
        title=f"Набор skills для УП #{plan_id}",
        source_type="curriculum_plan",
        source_id=plan_id,
        source_ref=f"curriculum_plan:{plan_id};brief:{brief_id}",
        description="Skills, на которых построен сохранённый черновик учебного плана.",
        metadata={
            "brief_id": brief_id,
            "plan_id": plan_id,
            "item_count": len(rows),
            "quality_metrics": quality_metrics,
        },
    )
    if skill_set_id is None:
        return {"status": "skipped", "plan_id": plan_id, "item_count": 0}
    items = [
        {
            "skill_id": int(row["skill_id"]),
            "suggestion_id": int(row["suggestion_id"]),
            "role": "target",
            "weight": 1.0,
            "sort_order": index,
            "rationale": f"curriculum_plan:{plan_id}",
        }
        for index, row in enumerate(rows, start=1)
    ]
    item_count = replace_skill_set_items(con, skill_set_id, items)
    return {"status": "synced", "plan_id": plan_id, "skill_set_id": skill_set_id, "item_count": item_count}


def promote_suggestion_to_catalog(con: sqlite3.Connection, suggestion_id: int) -> dict[str, object]:
    row = _load_skill_suggestion_row(con, suggestion_id)
    if not row:
        return {"status": "missing_suggestion", "suggestion_id": suggestion_id}
    if row["entity_type"] != "skill" or row["atomicity"] != "atomic":
        return {"status": "skipped_non_atomic", "suggestion_id": suggestion_id}
    if row["decision"] != "accepted":
        return {"status": "skipped_not_accepted", "suggestion_id": suggestion_id}

    existing_promotion = _existing_promotion(con, suggestion_id)
    normalized_name = _normalize_catalog_key(str(row["suggested_name"] or ""))
    if not normalized_name:
        return {"status": "skipped_empty_name", "suggestion_id": suggestion_id}

    skill_cols = _existing_cols(con, "skill")
    group_id = _ensure_skill_group(con, row["group_name"] or row["coverage_area"])
    skill_row = None
    created_skill = False
    if row["canonical_skill_id"] is not None:
        skill_row = _find_skill_by_id(con, int(row["canonical_skill_id"]))
    if skill_row is None:
        skill_row = _find_skill_by_normalized_name(con, normalized_name)
    if skill_row is None:
        columns = ["normalized_name", "canonical_name", "skill_type", "status"]
        values: list[object] = [normalized_name, str(row["suggested_name"]).strip(), "unknown", "active"]
        if "group_id" in skill_cols and group_id is not None:
            columns.append("group_id")
            values.append(group_id)
        if "code" in skill_cols:
            columns.append("code")
            values.append(f"skill-{_slug_catalog_key(str(row['suggested_name']))}")
        if "name" in skill_cols:
            columns.append("name")
            values.append(str(row["suggested_name"]).strip())
        if "resolution_status" in skill_cols:
            columns.append("resolution_status")
            values.append("manual")
        if "is_active" in skill_cols:
            columns.append("is_active")
            values.append(1)
        if "created_at" in skill_cols:
            columns.append("created_at")
            values.append(_utc_now_iso())
        if "updated_at" in skill_cols:
            columns.append("updated_at")
            values.append(_utc_now_iso())
        placeholders = ", ".join("?" for _ in columns)
        cur = con.execute(
            f"INSERT INTO skill({', '.join(columns)}) VALUES ({placeholders})",
            tuple(values),
        )
        skill_id = int(cur.lastrowid)
        skill_row = _find_skill_by_id(con, skill_id)
        created_skill = True
    else:
        skill_id = int(skill_row["id"])
        if str(skill_row["status"] or "active") != "active":
            con.execute("UPDATE skill SET status = 'active' WHERE id = ?", (skill_id,))
        updates = []
        params: list[object] = []
        if "is_active" in skill_cols:
            updates.append("is_active = 1")
        if "group_id" in skill_cols and group_id is not None:
            updates.append("group_id = COALESCE(group_id, ?)")
            params.append(group_id)
        if "name" in skill_cols:
            updates.append("name = COALESCE(NULLIF(name, ''), ?)")
            params.append(str(row["suggested_name"]).strip())
        if "updated_at" in skill_cols:
            updates.append("updated_at = ?")
            params.append(_utc_now_iso())
        if updates:
            params.append(skill_id)
            con.execute(f"UPDATE skill SET {', '.join(updates)} WHERE id = ?", tuple(params))

    created_alias = _ensure_skill_alias(con, skill_id, str(row["suggested_name"]), "intake_accept")
    source_name = str(row["source_name"] or "").strip()
    if source_name and source_name.casefold() != str(row["suggested_name"] or "").strip().casefold():
        created_alias = _ensure_skill_alias(con, skill_id, source_name, "intake_original") or created_alias
    canonical_name = str(skill_row["canonical_name"] if skill_row else row["suggested_name"])
    resolution_after = "matched" if _normalize_catalog_key(canonical_name) == normalized_name else "alias"
    con.execute(
        """
        UPDATE skill_suggestion
        SET canonical_skill_id = ?, resolution = ?, decision = 'accepted'
        WHERE id = ?
        """,
        (skill_id, resolution_after, suggestion_id),
    )
    if existing_promotion:
        con.execute(
            """
            UPDATE skill_promotion_log
            SET skill_id = ?,
                alias = ?,
                normalized_alias = ?,
                resolution_after_promotion = ?,
                created_skill = CASE WHEN created_skill = 1 OR ? = 1 THEN 1 ELSE 0 END,
                created_alias = CASE WHEN created_alias = 1 OR ? = 1 THEN 1 ELSE 0 END,
                status = 'active',
                reverted_at = NULL
            WHERE suggestion_id = ?
            """,
            (
                skill_id,
                str(row["suggested_name"]).strip(),
                normalized_name,
                resolution_after,
                1 if created_skill else 0,
                1 if created_alias else 0,
                suggestion_id,
            ),
        )
    else:
        con.execute(
            """
            INSERT INTO skill_promotion_log(
                suggestion_id, skill_id, alias, normalized_alias, resolution_after_promotion,
                created_skill, created_alias, status, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 'intake_accept')
            """,
            (
                suggestion_id,
                skill_id,
                str(row["suggested_name"]).strip(),
                normalized_name,
                resolution_after,
                1 if created_skill else 0,
                1 if created_alias else 0,
            ),
        )
    competency_link = competency_catalog.ensure_skill_competency_link(
        con,
        skill_id=skill_id,
        skill_name=str(row["suggested_name"]).strip(),
        competency_title=row["coverage_area"] or row["group_name"],
        indicators=row["indicators_json"],
        source_note=f"intake_accept:suggestion:{suggestion_id}",
    )
    skill_set = sync_brief_skill_set(con, int(row["brief_id"]))
    con.commit()
    return {
        "status": "promoted",
        "suggestion_id": suggestion_id,
        "skill_id": skill_id,
        "created_skill": created_skill,
        "created_alias": created_alias,
        "resolution_after": resolution_after,
        "competency_link": competency_link,
        "skill_set": skill_set,
    }


def revert_suggestion_promotion(con: sqlite3.Connection, suggestion_id: int) -> dict[str, object]:
    row = _load_skill_suggestion_row(con, suggestion_id)
    promotion = _existing_promotion(con, suggestion_id)
    if not row or not promotion or str(promotion["status"]) != "active":
        return {"status": "noop", "suggestion_id": suggestion_id}

    skill_id = int(promotion["skill_id"])
    normalized_alias = str(promotion["normalized_alias"] or "")

    if int(promotion["created_alias"] or 0) == 1 and normalized_alias:
        con.execute(
            "DELETE FROM skill_alias WHERE skill_id = ? AND normalized_alias = ?",
            (skill_id, normalized_alias),
        )

    should_disable_skill = False
    if int(promotion["created_skill"] or 0) == 1:
        active_promotions = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM skill_promotion_log
                WHERE skill_id = ?
                  AND status = 'active'
                  AND suggestion_id <> ?
                """,
                (skill_id, suggestion_id),
            ).fetchone()[0]
        )
        other_accepted_refs = int(
            con.execute(
                """
                SELECT COUNT(*)
                FROM skill_suggestion
                WHERE canonical_skill_id = ?
                  AND entity_type = 'skill'
                  AND atomicity = 'atomic'
                  AND decision = 'accepted'
                  AND id <> ?
                """,
                (skill_id, suggestion_id),
            ).fetchone()[0]
        )
        if active_promotions == 0 and other_accepted_refs == 0:
            should_disable_skill = True

    if should_disable_skill:
        skill_cols = _existing_cols(con, "skill")
        if "is_active" in skill_cols:
            con.execute("UPDATE skill SET status = 'candidate', is_active = 0 WHERE id = ?", (skill_id,))
        else:
            con.execute("UPDATE skill SET status = 'candidate' WHERE id = ?", (skill_id,))
        competency_catalog.remove_intake_competency_links_for_skill(con, skill_id)

    resolution_after = "new"
    canonical_skill_id: int | None = None
    fallback_skill = _find_skill_by_normalized_name(con, _normalize_catalog_key(str(row["suggested_name"] or "")))
    if fallback_skill and int(fallback_skill["id"]) != skill_id and str(fallback_skill["status"] or "active") == "active":
        canonical_skill_id = int(fallback_skill["id"])
        fallback_canonical = _normalize_catalog_key(str(fallback_skill["canonical_name"] or ""))
        resolution_after = "matched" if fallback_canonical == _normalize_catalog_key(str(row["suggested_name"] or "")) else "alias"

    con.execute(
        """
        UPDATE skill_suggestion
        SET canonical_skill_id = ?, resolution = ?
        WHERE id = ?
        """,
        (canonical_skill_id, resolution_after, suggestion_id),
    )
    con.execute(
        """
        UPDATE skill_promotion_log
        SET status = 'reverted',
            reverted_at = ?
        WHERE suggestion_id = ?
        """,
        (_utc_now_iso(), suggestion_id),
    )
    sync_brief_skill_set(con, int(row["brief_id"]))
    con.commit()
    return {
        "status": "reverted",
        "suggestion_id": suggestion_id,
        "skill_id": skill_id,
        "disabled_skill": should_disable_skill,
        "resolution_after": resolution_after,
    }


def link_suggestion_to_nearest(con: sqlite3.Connection, suggestion_id: int) -> dict[str, object]:
    """Accept coverage by the nearest existing catalog skill without creating a new skill."""
    row = con.execute(
        """
        SELECT ss.id, ss.nearest_skill_id, s.canonical_name
        FROM skill_suggestion ss
        LEFT JOIN skill s ON s.id = ss.nearest_skill_id
        WHERE ss.id = ?
        """,
        (suggestion_id,),
    ).fetchone()
    if not row or row["nearest_skill_id"] is None:
        return {"status": "missing_nearest", "suggestion_id": suggestion_id}
    con.execute(
        """
        UPDATE skill_suggestion
        SET canonical_skill_id = nearest_skill_id,
            resolution = 'alias'
        WHERE id = ?
        """,
        (suggestion_id,),
    )
    con.commit()
    return {
        "status": "linked",
        "suggestion_id": suggestion_id,
        "skill_id": int(row["nearest_skill_id"]),
        "canonical_name": row["canonical_name"],
    }


def sync_promotions_for_brief(con: sqlite3.Connection, brief_id: int) -> dict[str, int]:
    promoted = 0
    reverted = 0
    accepted_rows = con.execute(
        """
        SELECT id
        FROM skill_suggestion
        WHERE brief_id = ?
          AND entity_type = 'skill'
          AND atomicity = 'atomic'
          AND decision = 'accepted'
        ORDER BY id
        """,
        (brief_id,),
    ).fetchall()
    for row in accepted_rows:
        result = promote_suggestion_to_catalog(con, int(row["id"]))
        if result.get("status") == "promoted":
            promoted += 1

    active_rows = con.execute(
        """
        SELECT spl.suggestion_id
        FROM skill_promotion_log spl
        JOIN skill_suggestion ss ON ss.id = spl.suggestion_id
        WHERE ss.brief_id = ?
          AND spl.status = 'active'
          AND (ss.decision IS NULL OR ss.decision <> 'accepted')
        """,
        (brief_id,),
    ).fetchall()
    for row in active_rows:
        result = revert_suggestion_promotion(con, int(row["suggestion_id"]))
        if result.get("status") == "reverted":
            reverted += 1

    return {"promoted": promoted, "reverted": reverted}


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
        if (
            stored_decision == "needs_review"
            and c.atomicity == "composite"
            and "composite_decomposed" in (c.reasons or [])
        ):
            stored_decision = "superseded"
        if stored_decision == "superseded" and not allow_superseded:
            stored_decision = "rejected"
        con.execute(
            """INSERT INTO skill_suggestion(brief_id, suggested_name, source_name, group_name, coverage_area, bloom,
               indicators_json, tools, resolution, canonical_skill_id, match_score,
               nearest_skill_id, nearest_name, nearest_group, confidence, council_agreement,
               evidence_ids, decision, entity_type, atomicity, parent_suggestion_id, atomize_rationale)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                brief_id,
                c.name,
                c.source_name,
                c.group,
                c.coverage_area,
                max((i.bloom for i in c.indicators), default=None),
                json.dumps([indicator.model_dump(mode="json") for indicator in c.indicators], ensure_ascii=False),
                json.dumps(c.tools, ensure_ascii=False),
                c.resolution,
                c.canonical_skill_id,
                c.match_score,
                c.nearest_skill_id,
                c.nearest_name,
                c.nearest_group,
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
        if stored_decision == "needs_review":
            rq_entity_type = _review_queue_entity_type(c)
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
                (rq_entity_type, tmp_to_db[c.tmp_id], f"brief:{brief_id}", primary_reason, severity, details),
            )
    con.commit()
    return tmp_to_db


def save_prerequisites(
    con: sqlite3.Connection,
    brief_id: int,
    DAG,
    cands: list[SkillCandidate],
    tmp_to_db: dict[str, int] | None = None,
) -> int:
    by_tid = {c.tmp_id: c for c in cands}
    n = 0
    for u, v in DAG.edges():
        cu, cv = by_tid[u], by_tid[v]
        edge = DAG[u][v].get("edge")
        con.execute(
            """INSERT INTO skill_prerequisite(brief_id, src_skill_id, dst_skill_id, src_suggestion_id, dst_suggestion_id,
               src_name, dst_name, relation_type, confidence, source, review_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                brief_id,
                cu.canonical_skill_id,
                cv.canonical_skill_id,
                tmp_to_db.get(u) if tmp_to_db else None,
                tmp_to_db.get(v) if tmp_to_db else None,
                cu.name,
                cv.name,
                edge.relation_type if edge else "hard",
                DAG[u][v].get("conf"),
                edge.source if edge else "pipeline",
                "accepted" if (edge is None or edge.decision == "accept") else "needs_review",
            ),
        )
        n += 1
    con.commit()
    return n


def save_prerequisite_reviews(con: sqlite3.Connection, brief_id: int, edge_reviews: list[dict[str, object]]) -> int:
    count = 0
    decided_edge_keys: set[str] = set()
    if _table_exists(con, "prerequisite_edge_decision"):
        decided_edge_keys = {
            str(row["edge_key"])
            for row in con.execute(
                "SELECT edge_key FROM prerequisite_edge_decision WHERE brief_id = ?",
                (brief_id,),
            )
        }
    for item in edge_reviews:
        edge_key = str(item.get("edge_key") or "")
        if edge_key and edge_key in decided_edge_keys:
            continue
        con.execute(
            """
            INSERT INTO review_queue(entity_type, entity_id, source_ref, reason_code, severity, details, status)
            VALUES ('prerequisite_edge', NULL, ?, ?, ?, ?, 'open')
            """,
            (
                f"brief:{brief_id}",
                str(item.get("reason_code", "needs_review")),
                str(item.get("severity", "info")),
                json.dumps(
                    {
                        "review_kind": "prerequisite_edge",
                        "edge_key": edge_key,
                        "src_id": item.get("src_id"),
                        "dst_id": item.get("dst_id"),
                        "edge_label": item.get("edge_label"),
                        "confidence": item.get("confidence"),
                        "source": item.get("source"),
                        "relation_type": item.get("relation_type"),
                        "reasons": item.get("reasons") or [],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        count += 1
    con.commit()
    return count


def clear_curriculum_plan(con: sqlite3.Connection, brief_id: int, source_policy: str = "accepted_only") -> None:
    plan_rows = con.execute(
        "SELECT id FROM curriculum_plan WHERE brief_id = ? AND source_policy = ?",
        (brief_id, source_policy),
    ).fetchall()
    for row in plan_rows:
        if _table_exists(con, "skill_set") and _table_exists(con, "skill_set_item"):
            skill_set_rows = con.execute(
                "SELECT id FROM skill_set WHERE source_type = 'curriculum_plan' AND source_id = ?",
                (row["id"],),
            ).fetchall()
            for skill_set_row in skill_set_rows:
                con.execute("DELETE FROM skill_set_item WHERE skill_set_id = ?", (skill_set_row["id"],))
            con.execute("DELETE FROM skill_set WHERE source_type = 'curriculum_plan' AND source_id = ?", (row["id"],))
        con.execute("DELETE FROM curriculum_plan_row WHERE plan_id = ?", (row["id"],))
    con.execute(
        "DELETE FROM curriculum_plan WHERE brief_id = ? AND source_policy = ?",
        (brief_id, source_policy),
    )
    con.commit()


def save_curriculum_plan(
    con: sqlite3.Connection,
    brief_id: int,
    plan_payload: dict[str, object],
    source_policy: str = "accepted_only",
) -> dict[str, int]:
    clear_curriculum_plan(con, brief_id, source_policy)
    summary = plan_payload.get("summary") if isinstance(plan_payload.get("summary"), dict) else {}
    cur = con.execute(
        """
        INSERT INTO curriculum_plan(
            brief_id, source_policy, status, title, audience_level,
            total_blocks, total_projects, total_hours, total_days, total_xp, payload_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            brief_id,
            source_policy,
            str(plan_payload.get("status", "draft")),
            plan_payload.get("title"),
            plan_payload.get("audience_level"),
            int(summary.get("blocks", 0) or 0),
            int(summary.get("projects", 0) or 0),
            float(summary.get("total_hours", 0) or 0),
            float(summary.get("total_days", 0) or 0),
            int(summary.get("total_xp", 0) or 0),
            json.dumps(plan_payload, ensure_ascii=False),
        ),
    )
    plan_id = int(cur.lastrowid)
    row_count = 0
    for row in plan_payload.get("rows", []):
        if not isinstance(row, dict):
            continue
        effort_days = None if row.get("effort_days") in (None, "") else float(row.get("effort_days", 0) or 0)
        cumulative_days = None if row.get("cumulative_days") in (None, "") else float(row.get("cumulative_days", 0) or 0)
        xp = None if row.get("xp") in (None, "") else int(row.get("xp", 0) or 0)
        con.execute(
            """
            INSERT INTO curriculum_plan_row(
                plan_id, block_index, row_number, project_index_in_block, block_title, block_goal,
                project_name, project_summary, outcomes_know, outcomes_can, outcomes_skills,
                learning_outcomes, skills_list, audience_level, required_tools, materials,
                validation_criteria, storytelling, delivery_format, group_size, effort_hours, effort_days,
                cumulative_days, xp, completion_percent, p2p_checks, weighted_skills,
                platform_project_name, artifact_links
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                int(row.get("block_index", 0) or 0),
                int(row.get("row_number", 0) or 0),
                int(row.get("project_index_in_block", 0) or 0),
                row.get("block_title"),
                row.get("block_goal"),
                row.get("project_name"),
                row.get("project_summary"),
                row.get("outcomes_know"),
                row.get("outcomes_can"),
                row.get("outcomes_skills"),
                row.get("learning_outcomes"),
                row.get("skills_list"),
                row.get("audience_level"),
                row.get("required_tools"),
                row.get("materials"),
                row.get("validation_criteria"),
                row.get("storytelling"),
                row.get("delivery_format"),
                row.get("group_size"),
                float(row.get("effort_hours", 0) or 0),
                effort_days,
                cumulative_days,
                xp,
                None if row.get("completion_percent") in (None, "") else float(row.get("completion_percent", 0) or 0),
                None if row.get("p2p_checks") in (None, "") else int(row.get("p2p_checks", 0) or 0),
                row.get("weighted_skills"),
                row.get("platform_project_name"),
                row.get("artifact_links"),
            ),
        )
        row_count += 1
    skill_set = sync_curriculum_plan_skill_set(
        con,
        brief_id=brief_id,
        plan_id=plan_id,
        plan_payload=plan_payload,
    )
    con.commit()
    return {"plan_id": plan_id, "row_count": row_count, "skill_set_id": int(skill_set.get("skill_set_id") or 0)}
