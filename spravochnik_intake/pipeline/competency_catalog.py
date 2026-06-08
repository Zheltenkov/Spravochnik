"""Domain adapter for placing accepted intake skills into the competency catalog.

The flat `skill` table is not enough for the product workflow: methodologists
work with competencies, profile competencies, skill links and indicator rows.
This module keeps that structural logic outside the Flask app and outside LLM
pipeline stages so promotion can be tested and reused independently.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

SERVICE_PROFILE_SLUG = "intake-accepted-skills"
SERVICE_PROFILE_NAME = "Живой справочник intake"
SERVICE_SOURCE_ROOT = "intake://catalog"
SERVICE_WORKBOOK_PATH = "intake://accepted-skills"
SERVICE_WORKBOOK_NAME = "Accepted intake skills"
SERVICE_SHEET_NAME = "Accepted skills"
DEFAULT_COMPETENCY_TITLE = "Прочие компетенции"

REQUIRED_STRUCTURAL_TABLES = {
    "ingest_run",
    "source_workbook",
    "source_sheet",
    "source_block",
    "profile",
    "profile_source",
    "competency",
    "profile_competency",
    "competency_skill",
    "indicator_row",
    "dimension",
    "indicator",
}

BLOOM_TO_DIMENSION = {
    "remember": "knowledge",
    "understand": "knowledge",
    "apply": "ability",
    "analyze": "ability",
    "evaluate": "proficiency",
    "create": "proficiency",
}

DIMENSION_TITLES = {
    "knowledge": "Знает",
    "understanding": "Понимает",
    "ability": "Умеет",
    "proficiency": "Владеет",
    "unspecified": "Не указано",
}

INDICATOR_TYPE_TO_DIMENSION = {
    "знает": "knowledge",
    "понимает": "understanding",
    "умеет": "ability",
    "владеет": "proficiency",
}


@dataclass(frozen=True)
class CatalogContext:
    ingest_run_id: int
    source_workbook_id: int
    source_sheet_id: int
    profile_id: int


@dataclass(frozen=True)
class CompetencyLinkResult:
    status: str
    skill_id: int
    competency_id: int | None = None
    profile_competency_id: int | None = None
    competency_skill_id: int | None = None
    created_competency: bool = False
    created_profile_competency: bool = False
    created_competency_skill: bool = False
    needs_methodologist_review: bool = False
    created_review: bool = False
    created_indicator_rows: int = 0
    created_indicators: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _existing_cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}


def has_competency_structure(con: sqlite3.Connection) -> bool:
    return all(_table_exists(con, table) for table in REQUIRED_STRUCTURAL_TABLES)


def _normalize_text(value: object | None) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я+]+", " ", text)
    return " ".join(text.split())


def _clean_title(value: object | None, fallback: str = DEFAULT_COMPETENCY_TITLE) -> str:
    title = " ".join(str(value or "").split())
    return title or fallback


def _select_id(con: sqlite3.Connection, query: str, params: tuple[object, ...]) -> int | None:
    row = con.execute(query, params).fetchone()
    return int(row[0]) if row else None


def _ensure_ingest_run(con: sqlite3.Connection) -> int:
    existing_id = _select_id(
        con,
        "SELECT id FROM ingest_run WHERE source_root = ? AND status = 'completed' ORDER BY id LIMIT 1",
        (SERVICE_SOURCE_ROOT,),
    )
    if existing_id is not None:
        return existing_id
    cur = con.execute(
        """
        INSERT INTO ingest_run(started_at, finished_at, source_root, status, summary_json)
        VALUES (?, ?, ?, 'completed', ?)
        """,
        (
            _utc_now_iso(),
            _utc_now_iso(),
            SERVICE_SOURCE_ROOT,
            json.dumps({"source": "intake_accept"}, ensure_ascii=False),
        ),
    )
    return int(cur.lastrowid)


def _ensure_workbook(con: sqlite3.Connection, ingest_run_id: int) -> int:
    existing_id = _select_id(
        con,
        "SELECT id FROM source_workbook WHERE ingest_run_id = ? AND file_path = ?",
        (ingest_run_id, SERVICE_WORKBOOK_PATH),
    )
    if existing_id is not None:
        return existing_id
    cur = con.execute(
        """
        INSERT INTO source_workbook(ingest_run_id, file_path, file_name, sha256, last_modified_utc, source_kind)
        VALUES (?, ?, ?, ?, ?, 'draft')
        """,
        (
            ingest_run_id,
            SERVICE_WORKBOOK_PATH,
            SERVICE_WORKBOOK_NAME,
            "intake-accepted-skills",
            _utc_now_iso(),
        ),
    )
    return int(cur.lastrowid)


def _ensure_sheet(con: sqlite3.Connection, source_workbook_id: int) -> int:
    existing_id = _select_id(
        con,
        "SELECT id FROM source_sheet WHERE source_workbook_id = ? AND sheet_order = 1",
        (source_workbook_id,),
    )
    if existing_id is not None:
        return existing_id
    cur = con.execute(
        """
        INSERT INTO source_sheet(source_workbook_id, sheet_name, sheet_order, is_skipped, skip_reason)
        VALUES (?, ?, 1, 0, NULL)
        """,
        (source_workbook_id, SERVICE_SHEET_NAME),
    )
    return int(cur.lastrowid)


def _ensure_profile(con: sqlite3.Connection, source_workbook_id: int) -> int:
    profile_id = _select_id(con, "SELECT id FROM profile WHERE slug = ?", (SERVICE_PROFILE_SLUG,))
    if profile_id is None:
        cur = con.execute(
            """
            INSERT INTO profile(slug, name, source_kind, notes)
            VALUES (?, ?, 'draft', ?)
            """,
            (
                SERVICE_PROFILE_SLUG,
                SERVICE_PROFILE_NAME,
                "Служебный профиль для навыков, подтвержденных через intake.",
            ),
        )
        profile_id = int(cur.lastrowid)
    else:
        con.execute(
            "UPDATE profile SET name = ?, source_kind = 'draft' WHERE id = ?",
            (SERVICE_PROFILE_NAME, profile_id),
        )

    profile_source_id = _select_id(
        con,
        "SELECT id FROM profile_source WHERE profile_id = ? AND source_workbook_id = ?",
        (profile_id, source_workbook_id),
    )
    if profile_source_id is None:
        con.execute(
            """
            INSERT INTO profile_source(profile_id, source_workbook_id, version_label, is_primary)
            VALUES (?, ?, 'intake-live', 1)
            """,
            (profile_id, source_workbook_id),
        )
    return profile_id


def ensure_catalog_context(con: sqlite3.Connection) -> CatalogContext | None:
    if not has_competency_structure(con):
        return None
    ingest_run_id = _ensure_ingest_run(con)
    workbook_id = _ensure_workbook(con, ingest_run_id)
    sheet_id = _ensure_sheet(con, workbook_id)
    profile_id = _ensure_profile(con, workbook_id)
    return CatalogContext(
        ingest_run_id=ingest_run_id,
        source_workbook_id=workbook_id,
        source_sheet_id=sheet_id,
        profile_id=profile_id,
    )


def _ensure_competency(con: sqlite3.Connection, title: str) -> tuple[int, bool, str, str]:
    cleaned_title = _clean_title(title)
    normalized_title = _normalize_text(cleaned_title)
    existing = con.execute(
        """
        SELECT id, status
        FROM competency
        WHERE normalized_title = ?
        """,
        (normalized_title,),
    ).fetchone()
    if existing:
        # Never auto-activate a candidate competency. Promotion can update the title,
        # but methodologist confirmation is the only path to active status.
        con.execute("UPDATE competency SET title = ? WHERE id = ?", (cleaned_title, int(existing["id"])))
        return int(existing["id"]), False, cleaned_title, str(existing["status"] or "candidate")
    cur = con.execute(
        """
        INSERT INTO competency(normalized_title, title, description, status)
        VALUES (?, ?, ?, 'candidate')
        """,
        (
            normalized_title,
            cleaned_title,
            "Кандидат создан автоматически при подтверждении нового навыка из intake. Требует проверки методологом.",
        ),
    )
    return int(cur.lastrowid), True, cleaned_title, "candidate"


def _ensure_competency_review(
    con: sqlite3.Connection,
    *,
    competency_id: int,
    competency_title: str,
    source_note: str,
) -> bool:
    if not _table_exists(con, "review_queue"):
        return False
    existing_id = _select_id(
        con,
        """
        SELECT id
        FROM review_queue
        WHERE entity_type = 'competency'
          AND entity_id = ?
          AND reason_code = 'new_competency_candidate'
          AND status = 'open'
        ORDER BY id LIMIT 1
        """,
        (competency_id,),
    )
    if existing_id is not None:
        return False
    details = (
        f"Intake создал новую competency-кандидат «{competency_title}». "
        "Нужно подтвердить, что такой competency действительно нет в справочнике, "
        "или отклонить и вручную привязать skill к существующей competency."
    )
    con.execute(
        """
        INSERT INTO review_queue(entity_type, entity_id, source_ref, reason_code, severity, details, status)
        VALUES ('competency', ?, ?, 'new_competency_candidate', 'warning', ?, 'open')
        """,
        (competency_id, source_note, details),
    )
    return True


def _ensure_source_block(con: sqlite3.Connection, source_sheet_id: int, competency_title: str) -> int:
    existing_id = _select_id(
        con,
        "SELECT id FROM source_block WHERE source_sheet_id = ? AND raw_title = ? ORDER BY id LIMIT 1",
        (source_sheet_id, competency_title),
    )
    if existing_id is not None:
        return existing_id
    next_no = int(
        con.execute(
            "SELECT COALESCE(MAX(block_no), 0) + 10 FROM source_block WHERE source_sheet_id = ?",
            (source_sheet_id,),
        ).fetchone()[0]
        or 10
    )
    cur = con.execute(
        """
        INSERT INTO source_block(
            source_sheet_id, block_no, header_row_number, level_row_number,
            end_row_number, raw_title, raw_description, raw_prerequisites, raw_scale_signature
        )
        VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, NULL)
        """,
        (
            source_sheet_id,
            next_no,
            next_no,
            competency_title,
            "Служебный блок intake для подтвержденных навыков.",
        ),
    )
    return int(cur.lastrowid)


def _ensure_profile_competency(
    con: sqlite3.Connection,
    *,
    profile_id: int,
    competency_id: int,
    source_block_id: int,
    title: str,
    review_state: str,
) -> tuple[int, bool]:
    existing_id = _select_id(
        con,
        """
        SELECT id
        FROM profile_competency
        WHERE profile_id = ? AND competency_id = ?
        ORDER BY id LIMIT 1
        """,
        (profile_id, competency_id),
    )
    if existing_id is not None:
        if review_state == "accepted":
            con.execute(
                """
                UPDATE profile_competency
                SET title_in_source = COALESCE(NULLIF(title_in_source, ''), ?),
                    review_state = 'accepted'
                WHERE id = ?
                """,
                (title, existing_id),
            )
        else:
            con.execute(
                """
                UPDATE profile_competency
                SET title_in_source = COALESCE(NULLIF(title_in_source, ''), ?),
                    review_state = CASE WHEN review_state = 'accepted' THEN 'accepted' ELSE ? END
                WHERE id = ?
                """,
                (title, review_state, existing_id),
            )
        return existing_id, False

    next_order = int(
        con.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM profile_competency WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()[0]
        or 10
    )
    cur = con.execute(
        """
        INSERT INTO profile_competency(
            profile_id, competency_id, source_block_id, scale_id, title_in_source,
            description_in_source, prerequisites_text, sort_order, review_state
        )
        VALUES (?, ?, ?, NULL, ?, NULL, NULL, ?, ?)
        """,
        (profile_id, competency_id, source_block_id, title, next_order, review_state),
    )
    return int(cur.lastrowid), True


def _ensure_competency_skill(
    con: sqlite3.Connection,
    *,
    profile_competency_id: int,
    skill_id: int,
    skill_name: str,
    review_state: str,
) -> tuple[int, bool]:
    existing_id = _select_id(
        con,
        """
        SELECT id
        FROM competency_skill
        WHERE profile_competency_id = ? AND skill_id = ?
        ORDER BY id LIMIT 1
        """,
        (profile_competency_id, skill_id),
    )
    if existing_id is not None:
        if review_state == "accepted":
            con.execute(
                """
                UPDATE competency_skill
                SET source_skill_name = ?,
                    review_state = 'accepted'
                WHERE id = ?
                """,
                (skill_name, existing_id),
            )
        else:
            con.execute(
                """
                UPDATE competency_skill
                SET source_skill_name = ?,
                    review_state = CASE WHEN review_state = 'accepted' THEN 'accepted' ELSE ? END
                WHERE id = ?
                """,
                (skill_name, review_state, existing_id),
            )
        return existing_id, False

    next_order = int(
        con.execute(
            "SELECT COALESCE(MAX(skill_order), 0) + 10 FROM competency_skill WHERE profile_competency_id = ?",
            (profile_competency_id,),
        ).fetchone()[0]
        or 10
    )
    cur = con.execute(
        """
        INSERT INTO competency_skill(profile_competency_id, skill_id, source_skill_name, skill_order, review_state)
        VALUES (?, ?, ?, ?, ?)
        """,
        (profile_competency_id, skill_id, skill_name, next_order, review_state),
    )
    return int(cur.lastrowid), True


def _ensure_dimension(con: sqlite3.Connection, code: str) -> int:
    code = code if code in DIMENSION_TITLES else "unspecified"
    existing_id = _select_id(con, "SELECT id FROM dimension WHERE code = ?", (code,))
    if existing_id is not None:
        return existing_id
    cur = con.execute(
        "INSERT INTO dimension(code, title) VALUES (?, ?)",
        (code, DIMENSION_TITLES[code]),
    )
    return int(cur.lastrowid)


def _dimension_code_from_indicator(indicator: dict[str, Any]) -> str:
    explicit_type = _normalize_text(indicator.get("indicator_type") or indicator.get("type"))
    if explicit_type in INDICATOR_TYPE_TO_DIMENSION:
        return INDICATOR_TYPE_TO_DIMENSION[explicit_type]
    bloom = str(indicator.get("bloom") or "").strip().lower()
    return BLOOM_TO_DIMENSION.get(bloom, "unspecified")


def _indicator_type_for_dimension(code: str) -> str:
    return DIMENSION_TITLES.get(code, DIMENSION_TITLES["unspecified"])


def _indicator_text(indicator: dict[str, Any]) -> str:
    return " ".join(str(indicator.get("text") or "").split())


def _parse_indicators(raw_indicators: object | None) -> list[dict[str, Any]]:
    if raw_indicators is None:
        return []
    if isinstance(raw_indicators, str):
        try:
            raw_indicators = json.loads(raw_indicators)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw_indicators, list):
        return []
    parsed: list[dict[str, Any]] = []
    for item in raw_indicators:
        if isinstance(item, dict):
            parsed.append(item)
    return parsed


def _load_existing_indicator_specs(con: sqlite3.Connection, skill_id: int) -> list[dict[str, Any]]:
    if not _table_exists(con, "indicator"):
        return []
    return [
        {
            "text": row["text"],
            "indicator_type": row["indicator_type"],
            "bloom": None,
        }
        for row in con.execute(
            """
            SELECT indicator_type, text
            FROM indicator
            WHERE skill_id = ? AND is_active = 1
            ORDER BY sort_order, id
            """,
            (skill_id,),
        )
    ]


def _next_indicator_row_number(con: sqlite3.Connection, competency_skill_id: int) -> int:
    return int(
        con.execute(
            "SELECT COALESCE(MAX(source_row_number), 0) + 1 FROM indicator_row WHERE competency_skill_id = ?",
            (competency_skill_id,),
        ).fetchone()[0]
        or 1
    )


def _ensure_indicator_row(
    con: sqlite3.Connection,
    *,
    competency_skill_id: int,
    dimension_id: int,
    text: str,
    source_note: str,
) -> tuple[int, bool]:
    existing_id = _select_id(
        con,
        """
        SELECT id
        FROM indicator_row
        WHERE competency_skill_id = ?
          AND dimension_id = ?
          AND COALESCE(base_text, '') = ?
        ORDER BY id LIMIT 1
        """,
        (competency_skill_id, dimension_id, text),
    )
    if existing_id is not None:
        return existing_id, False
    row_number = _next_indicator_row_number(con, competency_skill_id)
    cur = con.execute(
        """
        INSERT INTO indicator_row(
            competency_skill_id, dimension_id, source_row_number,
            inherited_skill, inherited_dimension, base_text, raw_number, notes
        )
        VALUES (?, ?, ?, 0, 0, ?, NULL, ?)
        """,
        (competency_skill_id, dimension_id, row_number, text, source_note),
    )
    return int(cur.lastrowid), True


def _level_label_for_dimension(dimension_code: str) -> str:
    if dimension_code == "knowledge":
        return "Знает"
    if dimension_code == "ability":
        return "Умеет"
    return "Владеет"


def _ensure_indicator_level_cell(
    con: sqlite3.Connection,
    *,
    indicator_row_id: int,
    raw_level_label: str,
    raw_value: str,
) -> bool:
    if not _table_exists(con, "indicator_level_cell"):
        return False
    existing_id = _select_id(
        con,
        """
        SELECT id
        FROM indicator_level_cell
        WHERE indicator_row_id = ?
          AND raw_level_label = ?
          AND raw_value = ?
        ORDER BY id LIMIT 1
        """,
        (indicator_row_id, raw_level_label, raw_value),
    )
    if existing_id is not None:
        return False
    next_order = int(
        con.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM indicator_level_cell WHERE indicator_row_id = ?",
            (indicator_row_id,),
        ).fetchone()[0]
        or 1
    )
    con.execute(
        """
        INSERT INTO indicator_level_cell(
            indicator_row_id, proficiency_level_id, raw_level_label, raw_value, value_kind, sort_order
        )
        VALUES (?, NULL, ?, ?, 'text', ?)
        """,
        (indicator_row_id, raw_level_label, raw_value, next_order),
    )
    return True


def _ensure_flat_indicator(
    con: sqlite3.Connection,
    *,
    skill_id: int,
    indicator_type: str,
    text: str,
    source_indicator_row_id: int | None,
) -> bool:
    normalized_text = _normalize_text(text)
    existing = con.execute(
        """
        SELECT id, is_active, source_indicator_row_id
        FROM indicator
        WHERE skill_id = ? AND indicator_type = ? AND normalized_text = ?
        ORDER BY id LIMIT 1
        """,
        (skill_id, indicator_type, normalized_text),
    ).fetchone()
    if existing:
        updates = ["is_active = 1", "updated_at = ?"]
        params: list[object] = [_utc_now_iso()]
        if source_indicator_row_id is not None and existing["source_indicator_row_id"] is None:
            updates.append("source_indicator_row_id = ?")
            params.append(source_indicator_row_id)
        params.append(int(existing["id"]))
        con.execute(f"UPDATE indicator SET {', '.join(updates)} WHERE id = ?", tuple(params))
        return False

    next_order = int(
        con.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM indicator WHERE skill_id = ?",
            (skill_id,),
        ).fetchone()[0]
        or 1
    )
    indicator_cols = _existing_cols(con, "indicator")
    columns = [
        "skill_id",
        "indicator_type",
        "text",
        "normalized_text",
        "sort_order",
        "source_indicator_row_id",
        "source_profile_name",
        "source_scale_title",
        "is_active",
    ]
    values: list[object] = [
        skill_id,
        indicator_type,
        text,
        normalized_text,
        next_order,
        source_indicator_row_id,
        SERVICE_PROFILE_NAME,
        "intake-live",
        1,
    ]
    if "created_at" in indicator_cols:
        columns.append("created_at")
        values.append(_utc_now_iso())
    if "updated_at" in indicator_cols:
        columns.append("updated_at")
        values.append(_utc_now_iso())
    placeholders = ", ".join("?" for _ in columns)
    con.execute(
        f"INSERT INTO indicator({', '.join(columns)}) VALUES ({placeholders})",
        tuple(values),
    )
    return True


def _ensure_indicators(
    con: sqlite3.Connection,
    *,
    skill_id: int,
    competency_skill_id: int,
    indicators: object | None,
    source_note: str,
) -> tuple[int, int]:
    specs = _parse_indicators(indicators)
    if indicators is None:
        specs = _load_existing_indicator_specs(con, skill_id)

    created_rows = 0
    created_flat = 0
    for spec in specs:
        text = _indicator_text(spec)
        if not text:
            continue
        dimension_code = _dimension_code_from_indicator(spec)
        dimension_id = _ensure_dimension(con, dimension_code)
        row_id, row_created = _ensure_indicator_row(
            con,
            competency_skill_id=competency_skill_id,
            dimension_id=dimension_id,
            text=text,
            source_note=source_note,
        )
        if row_created:
            created_rows += 1
        _ensure_indicator_level_cell(
            con,
            indicator_row_id=row_id,
            raw_level_label=_level_label_for_dimension(dimension_code),
            raw_value=text,
        )
        if _ensure_flat_indicator(
            con,
            skill_id=skill_id,
            indicator_type=_indicator_type_for_dimension(dimension_code),
            text=text,
            source_indicator_row_id=row_id,
        ):
            created_flat += 1
    return created_rows, created_flat


def ensure_skill_competency_link(
    con: sqlite3.Connection,
    *,
    skill_id: int,
    skill_name: str,
    competency_title: str | None,
    indicators: object | None,
    source_note: str = "intake_accept",
) -> dict[str, object]:
    context = ensure_catalog_context(con)
    if context is None:
        return CompetencyLinkResult(status="skipped_missing_structure", skill_id=skill_id).to_dict()

    competency_id, created_competency, clean_title, competency_status = _ensure_competency(con, _clean_title(competency_title))
    needs_review = competency_status != "active"
    link_review_state = "needs_review" if needs_review else "accepted"
    created_review = False
    if needs_review:
        created_review = _ensure_competency_review(
            con,
            competency_id=competency_id,
            competency_title=clean_title,
            source_note=source_note,
        )
    source_block_id = _ensure_source_block(con, context.source_sheet_id, clean_title)
    profile_competency_id, created_profile_competency = _ensure_profile_competency(
        con,
        profile_id=context.profile_id,
        competency_id=competency_id,
        source_block_id=source_block_id,
        title=clean_title,
        review_state=link_review_state,
    )
    competency_skill_id, created_competency_skill = _ensure_competency_skill(
        con,
        profile_competency_id=profile_competency_id,
        skill_id=skill_id,
        skill_name=skill_name,
        review_state=link_review_state,
    )
    created_rows, created_indicators = _ensure_indicators(
        con,
        skill_id=skill_id,
        competency_skill_id=competency_skill_id,
        indicators=indicators,
        source_note=source_note,
    )
    return CompetencyLinkResult(
        status="linked",
        skill_id=skill_id,
        competency_id=competency_id,
        profile_competency_id=profile_competency_id,
        competency_skill_id=competency_skill_id,
        created_competency=created_competency,
        created_profile_competency=created_profile_competency,
        created_competency_skill=created_competency_skill,
        needs_methodologist_review=needs_review,
        created_review=created_review,
        created_indicator_rows=created_rows,
        created_indicators=created_indicators,
    ).to_dict()


def resolve_competency_candidate(
    con: sqlite3.Connection,
    *,
    competency_id: int,
    accepted: bool,
) -> dict[str, object]:
    if not has_competency_structure(con):
        return {"status": "skipped_missing_structure", "competency_id": competency_id}
    existing = con.execute(
        "SELECT id, status FROM competency WHERE id = ?",
        (competency_id,),
    ).fetchone()
    if not existing:
        return {"status": "missing", "competency_id": competency_id}

    if accepted:
        con.execute("UPDATE competency SET status = 'active' WHERE id = ?", (competency_id,))
        con.execute(
            """
            UPDATE profile_competency
            SET review_state = 'accepted'
            WHERE competency_id = ?
              AND review_state = 'needs_review'
            """,
            (competency_id,),
        )
        con.execute(
            """
            UPDATE competency_skill
            SET review_state = 'accepted'
            WHERE profile_competency_id IN (
                SELECT id
                FROM profile_competency
                WHERE competency_id = ?
            )
              AND review_state = 'needs_review'
            """,
            (competency_id,),
        )
        return {"status": "accepted", "competency_id": competency_id}

    con.execute("UPDATE competency SET status = 'deprecated' WHERE id = ?", (competency_id,))
    con.execute(
        "UPDATE profile_competency SET review_state = 'draft' WHERE competency_id = ?",
        (competency_id,),
    )
    con.execute(
        """
        UPDATE competency_skill
        SET review_state = 'draft'
        WHERE profile_competency_id IN (
            SELECT id
            FROM profile_competency
            WHERE competency_id = ?
        )
        """,
        (competency_id,),
    )
    return {"status": "rejected", "competency_id": competency_id}


def reopen_competency_candidate(con: sqlite3.Connection, *, competency_id: int) -> dict[str, object]:
    if not has_competency_structure(con):
        return {"status": "skipped_missing_structure", "competency_id": competency_id}
    existing = con.execute(
        "SELECT id FROM competency WHERE id = ?",
        (competency_id,),
    ).fetchone()
    if not existing:
        return {"status": "missing", "competency_id": competency_id}
    con.execute("UPDATE competency SET status = 'candidate' WHERE id = ?", (competency_id,))
    con.execute(
        "UPDATE profile_competency SET review_state = 'needs_review' WHERE competency_id = ?",
        (competency_id,),
    )
    con.execute(
        """
        UPDATE competency_skill
        SET review_state = 'needs_review'
        WHERE profile_competency_id IN (
            SELECT id
            FROM profile_competency
            WHERE competency_id = ?
        )
        """,
        (competency_id,),
    )
    return {"status": "reopened", "competency_id": competency_id}


def remove_intake_competency_links_for_skill(con: sqlite3.Connection, skill_id: int) -> int:
    context = ensure_catalog_context(con)
    if context is None:
        return 0
    rows = con.execute(
        """
        SELECT cs.id
        FROM competency_skill cs
        JOIN profile_competency pc ON pc.id = cs.profile_competency_id
        WHERE pc.profile_id = ? AND cs.skill_id = ?
        """,
        (context.profile_id, skill_id),
    ).fetchall()
    for row in rows:
        con.execute("DELETE FROM competency_skill WHERE id = ?", (int(row["id"]),))
    return len(rows)


def remove_competency_skill_link(con: sqlite3.Connection, competency_skill_id: int) -> dict[str, object]:
    if not _table_exists(con, "competency_skill"):
        return {"status": "skipped_missing_structure", "competency_skill_id": competency_skill_id}
    existing = con.execute(
        "SELECT id, skill_id FROM competency_skill WHERE id = ?",
        (competency_skill_id,),
    ).fetchone()
    if not existing:
        return {"status": "missing", "competency_skill_id": competency_skill_id}
    con.execute("DELETE FROM competency_skill WHERE id = ?", (competency_skill_id,))
    con.commit()
    return {
        "status": "removed",
        "competency_skill_id": competency_skill_id,
        "skill_id": existing["skill_id"],
    }


def list_skill_competency_links(con: sqlite3.Connection, skill_id: int) -> list[dict[str, object]]:
    if not has_competency_structure(con):
        return []
    return [
        dict(row)
        for row in con.execute(
            """
            SELECT
                cs.id AS competency_skill_id,
                cs.review_state AS competency_skill_state,
                cs.skill_order,
                pc.id AS profile_competency_id,
                pc.review_state AS profile_competency_state,
                c.id AS competency_id,
                c.title AS competency_title,
                c.status AS competency_status,
                p.id AS profile_id,
                p.name AS profile_name,
                COUNT(ir.id) AS indicator_row_count
            FROM competency_skill cs
            JOIN profile_competency pc ON pc.id = cs.profile_competency_id
            JOIN competency c ON c.id = pc.competency_id
            JOIN profile p ON p.id = pc.profile_id
            LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
            WHERE cs.skill_id = ?
            GROUP BY cs.id, pc.id, c.id, p.id
            ORDER BY p.name, c.title, cs.skill_order, cs.id
            """,
            (skill_id,),
        )
    ]


def list_competency_options(con: sqlite3.Connection, query: str = "", limit: int = 40) -> list[dict[str, object]]:
    if not _table_exists(con, "competency"):
        return []
    cleaned_query = _normalize_text(query)
    if cleaned_query:
        return [
            dict(row)
            for row in con.execute(
                """
                SELECT id, title, status
                FROM competency
                WHERE normalized_title LIKE ?
                ORDER BY title
                LIMIT ?
                """,
                (f"%{cleaned_query}%", limit),
            )
        ]
    return [
        dict(row)
        for row in con.execute(
            """
            SELECT id, title, status
            FROM competency
            ORDER BY title
            LIMIT ?
            """,
            (limit,),
        )
    ]
