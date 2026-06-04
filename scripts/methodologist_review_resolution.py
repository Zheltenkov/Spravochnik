from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


SUMMARY_JSON = Path(__file__).resolve().parents[1] / "artifacts" / "catalog_summary.json"

LEGACY_IGNORE_WORKBOOKS = {
    "Шаблон Компетентностного профиля.xlsx",
}

NONSTANDARD_SHEET_REFS = {
    "Компетентностный профиль Разработчик на C++.xlsx::ML на C++",
    "Компетентностный профиль Разработчик на C++.xlsx::Графика и GameDev",
    "Компетентностный профиль Разработчик на C++.xlsx::Низкоуровневая разработка на C+",
}

BLOCK_RENAMES = {
    "Работа с реляционными БД - исключить?": "Работа с реляционными БД",
    "Работа с нереляционными БД - исключить?": "Работа с нереляционными БД",
}

SKILL_RENAMES = {
    "Быть готовым быть T-shape?": "Готовность к T-shaped развитию",
    "Прототипирование?? Создание прототипов в Figma": "Создание прототипов в Figma",
}

PM_DIMENSION_RULES_BY_SKILL_ID = {
    92: "knowledge",
    93: "ability",
    94: "ability",
    95: "ability",
    96: "ability",
    97: "ability",
    98: "ability",
    99: "ability",
    100: "ability",
    101: "ability",
    104: "proficiency",
    105: "proficiency",
    106: "proficiency",
    107: "proficiency",
    110: "proficiency",
}

PM_DIMENSION_RULES_BY_ROW_ID = {
    942: "knowledge",
    979: "ability",
    980: "ability",
    981: "knowledge",
    982: "knowledge",
    983: "knowledge",
    984: "knowledge",
    985: "knowledge",
    986: "knowledge",
    987: "knowledge",
    988: "knowledge",
    989: "ability",
    990: "ability",
    991: "ability",
    992: "ability",
    993: "ability",
    994: "ability",
    1009: "knowledge",
    1010: "proficiency",
}

PM_DOCUMENTING_KNOWLEDGE_ROWS = {1011, 1012, 1013, 1014, 1015, 1016, 1017}


def normalize_key(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def parse_workbook_name(source_ref: str | None) -> str:
    if not source_ref:
        return ""
    return str(source_ref).split("::", 1)[0].strip()


def touch_review(
    conn: sqlite3.Connection,
    review_id: int,
    status: str,
    note: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    reviewed_at = now if status != "open" else None
    conn.execute(
        """
        UPDATE review_queue
        SET status = ?,
            resolution_note = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (status, note, reviewed_at, now, review_id),
    )


def update_summary_open_reviews(conn: sqlite3.Connection, summary_json: Path = SUMMARY_JSON) -> None:
    if not summary_json.exists():
        return
    payload = json.loads(summary_json.read_text(encoding="utf-8"))
    open_count = int(conn.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'open'").fetchone()[0])
    payload.setdefault("counts", {})["open_reviews"] = open_count
    payload["generated_at_utc"] = datetime.now(UTC).isoformat()
    summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def rename_skill(conn: sqlite3.Connection, old_name: str, new_name: str) -> int:
    rows = conn.execute("SELECT id FROM skill WHERE canonical_name = ?", (old_name,)).fetchall()
    if not rows:
        return 0
    normalized_new = normalize_key(new_name)
    updated = 0
    for row in rows:
        skill_id = int(row[0])
        conn.execute(
            """
            INSERT OR IGNORE INTO skill_alias(skill_id, alias, normalized_alias, source)
            VALUES(?, ?, ?, 'methodologist_resolution')
            """,
            (skill_id, old_name, normalize_key(old_name)),
        )
        conn.execute(
            """
            UPDATE skill
            SET canonical_name = ?, normalized_name = ?
            WHERE id = ?
            """,
            (new_name, normalized_new, skill_id),
        )
        conn.execute(
            """
            UPDATE competency_skill
            SET source_skill_name = ?
            WHERE skill_id = ? AND source_skill_name = ?
            """,
            (new_name, skill_id, old_name),
        )
        updated += 1
    return updated


def rename_block(conn: sqlite3.Connection, old_title: str, new_title: str) -> int:
    rows = conn.execute("SELECT id FROM source_block WHERE raw_title = ?", (old_title,)).fetchall()
    if not rows:
        return 0
    updated = 0
    for row in rows:
        block_id = int(row[0])
        conn.execute("UPDATE source_block SET raw_title = ? WHERE id = ?", (new_title, block_id))
        conn.execute(
            """
            UPDATE profile_competency
            SET title_in_source = ?,
                review_state = 'accepted'
            WHERE source_block_id = ?
            """,
            (new_title, block_id),
        )
        updated += 1
    return updated


def classify_missing_dimension(row: sqlite3.Row) -> str | None:
    workbook = parse_workbook_name(row["source_ref"])
    skill_id = int(row["competency_skill_id"])
    indicator_row_id = int(row["indicator_row_id"])

    if workbook in LEGACY_IGNORE_WORKBOOKS:
        return None
    if workbook == "Компетентностный профиль Data Scientist-2.xlsx":
        return None

    if workbook == "сырой Компетентностный профиль Разработчик на Си.xlsx":
        return "ability"

    if workbook != "v1 Компетентностный профиль Project manager .xlsx":
        return None

    if indicator_row_id in PM_DIMENSION_RULES_BY_ROW_ID:
        return PM_DIMENSION_RULES_BY_ROW_ID[indicator_row_id]

    if skill_id == 109:
        return "knowledge" if indicator_row_id in PM_DOCUMENTING_KNOWLEDGE_ROWS else "ability"

    if skill_id in PM_DIMENSION_RULES_BY_SKILL_ID:
        return PM_DIMENSION_RULES_BY_SKILL_ID[skill_id]

    return None


def apply_methodologist_review_decisions(conn: sqlite3.Connection) -> dict[str, int]:
    conn.row_factory = sqlite3.Row
    metrics = {
        "resolved": 0,
        "ignored": 0,
        "renamed_skills": 0,
        "renamed_blocks": 0,
        "dimensions_set": 0,
    }

    dimension_map = {
        row["code"]: int(row["id"])
        for row in conn.execute("SELECT id, code FROM dimension")
    }

    for old_title, new_title in BLOCK_RENAMES.items():
        metrics["renamed_blocks"] += rename_block(conn, old_title, new_title)

    for old_name, new_name in SKILL_RENAMES.items():
        metrics["renamed_skills"] += rename_skill(conn, old_name, new_name)

    missing_dimension_rows = conn.execute(
        """
        SELECT
            rq.id AS review_id,
            rq.source_ref,
            ir.id AS indicator_row_id,
            cs.id AS competency_skill_id
        FROM review_queue rq
        JOIN indicator_row ir ON ir.id = rq.entity_id
        JOIN competency_skill cs ON cs.id = ir.competency_skill_id
        WHERE rq.reason_code = 'missing_dimension'
        ORDER BY rq.id
        """
    ).fetchall()

    for row in missing_dimension_rows:
        dimension_code = classify_missing_dimension(row)
        if dimension_code is None:
            workbook = parse_workbook_name(row["source_ref"])
            if workbook in LEGACY_IGNORE_WORKBOOKS:
                touch_review(
                    conn,
                    int(row["review_id"]),
                    "ignored",
                    "Шаблонный источник не используется для рабочего каталога. Кейс снят с ручной проверки.",
                )
                metrics["ignored"] += 1
            elif workbook == "Компетентностный профиль Data Scientist-2.xlsx":
                touch_review(
                    conn,
                    int(row["review_id"]),
                    "ignored",
                    "Фрагмент признан несогласованным с блоком компетенции в устаревшем источнике. В рабочий каталог не переносим.",
                )
                metrics["ignored"] += 1
            continue

        conn.execute(
            """
            UPDATE indicator_row
            SET dimension_id = ?,
                inherited_dimension = 0
            WHERE id = ?
            """,
            (dimension_map[dimension_code], int(row["indicator_row_id"])),
        )
        dimension_title = {
            "knowledge": "Знает",
            "ability": "Умеет",
            "proficiency": "Владеет",
        }[dimension_code]
        touch_review(
            conn,
            int(row["review_id"]),
            "resolved",
            f"Методологически проставлен тип индикатора: «{dimension_title}».",
        )
        metrics["resolved"] += 1
        metrics["dimensions_set"] += 1

    review_rows = conn.execute(
        """
        SELECT id, reason_code, source_ref
        FROM review_queue
        ORDER BY id
        """
    ).fetchall()

    for row in review_rows:
        review_id = int(row["id"])
        reason_code = str(row["reason_code"])
        source_ref = str(row["source_ref"] or "")

        if reason_code == "missing_dimension":
            continue

        if reason_code == "level_headers_inherited_from_previous_block":
            touch_review(
                conn,
                review_id,
                "resolved",
                "Шкала подтверждена методологически. Блок оставлен в работе с унаследованной шкалой.",
            )
            metrics["resolved"] += 1
            continue

        if reason_code == "ambiguous_block_title":
            touch_review(
                conn,
                review_id,
                "resolved",
                "Название блока очищено от служебной пометки и подтверждено для каталога.",
            )
            metrics["resolved"] += 1
            continue

        if reason_code == "ambiguous_skill_name":
            touch_review(
                conn,
                review_id,
                "resolved",
                "Название skill нормализовано и утверждено методологически.",
            )
            metrics["resolved"] += 1
            continue

        if reason_code == "missing_block_title":
            touch_review(
                conn,
                review_id,
                "ignored",
                "Шаблонный блок без названия не используется в рабочем каталоге. Кейс снят с ручной проверки.",
            )
            metrics["ignored"] += 1
            continue

        if reason_code == "orphan_indicator_row":
            touch_review(
                conn,
                review_id,
                "ignored",
                "Строка относится к шаблонному источнику без рабочего значения для каталога. В импорт не включаем.",
            )
            metrics["ignored"] += 1
            continue

        if reason_code == "no_header_rows":
            if source_ref in NONSTANDARD_SHEET_REFS:
                touch_review(
                    conn,
                    review_id,
                    "ignored",
                    "Лист признан нецелевым для текущего формата импорта. Его нужно обрабатывать отдельно, поэтому из текущей очереди он снят.",
                )
                metrics["ignored"] += 1
            continue

    conn.commit()
    return metrics
