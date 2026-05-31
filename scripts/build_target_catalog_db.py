from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


DEFAULT_SOURCE_DB = Path("artifacts/skills_catalog.sqlite")
DEFAULT_TARGET_DB = Path("artifacts/target_catalog.sqlite")
DEFAULT_SUMMARY = Path("artifacts/target_catalog_summary.json")
COMPARE_REPORT = Path("artifacts/live_catalog_comparison.json")
LIVE_SNAPSHOT = Path("artifacts/live_catalog_snapshot.json")

CANONICAL_BAND_ORDER = {
    "trainee": 1,
    "junior_minus": 2,
    "junior": 3,
    "basic": 4,
    "junior_plus": 5,
    "middle": 6,
    "senior": 7,
    "master": 8,
}

CANONICAL_BAND_LABEL = {
    "trainee": "Стажер",
    "junior_minus": "Начальный (junior-)",
    "junior": "Начальный (junior)",
    "basic": "Базовый",
    "junior_plus": "Базовый (junior+)",
    "middle": "Продвинутый (middle)",
    "senior": "Продвинутый (senior)",
    "master": "Мастерский",
}


def normalize_key(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    return " ".join(value.split())


def slugify(value: str) -> str:
    value = normalize_key(value)
    value = re.sub(r"[^\w]+", "-", value, flags=re.UNICODE)
    return value.strip("-") or "item"


def source_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(row[1] == column_name for row in conn.execute(f"PRAGMA table_info({table_name})"))


def complexity_label_for_band(band: str | None) -> str | None:
    if not band:
        return None
    return CANONICAL_BAND_LABEL.get(band, band.replace("_", " "))


def build_complexity_summary(
    min_band: str | None,
    max_band: str | None,
    min_label: str | None,
    max_label: str | None,
) -> str | None:
    if not min_band and not max_band:
        return None
    start_label = min_label or complexity_label_for_band(min_band)
    end_label = max_label or complexity_label_for_band(max_band)
    if not start_label:
        return end_label
    if not end_label or start_label == end_label:
        return start_label
    return f"{start_label} -> {end_label}"


def choose_profile_name(conn: sqlite3.Connection, profile_like: str | None) -> str:
    preferred_name = None
    if COMPARE_REPORT.exists():
        report = json.loads(COMPARE_REPORT.read_text(encoding="utf-8"))
        preferred_name = report.get("profile_name")
        if preferred_name:
            row = conn.execute("SELECT name FROM profile WHERE name = ?", (preferred_name,)).fetchone()
            if row:
                return row[0]

    pattern = profile_like or "%Java%"
    row = conn.execute("SELECT name FROM profile WHERE name LIKE ? ORDER BY id LIMIT 1", (pattern,)).fetchone()
    if not row:
        raise RuntimeError(f"No profile matched {pattern!r}")
    return row[0]


def load_indicator_map(source_conn: sqlite3.Connection, profile_name: str) -> dict[int, list[tuple[str, str]]]:
    rows = source_conn.execute(
        """
        SELECT
            s.id AS skill_id,
            COALESCE(d.title, 'Не указано') AS indicator_type,
            ilc.raw_value AS indicator_text
        FROM profile p
        JOIN profile_competency pc ON pc.profile_id = p.id
        JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        JOIN skill s ON s.id = cs.skill_id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        LEFT JOIN dimension d ON d.id = ir.dimension_id
        LEFT JOIN indicator_level_cell ilc ON ilc.indicator_row_id = ir.id
        WHERE p.name = ?
          AND COALESCE(TRIM(ilc.raw_value), '') <> ''
        ORDER BY
            s.canonical_name,
            CASE COALESCE(d.title, '')
                WHEN 'Знает' THEN 1
                WHEN 'Умеет' THEN 2
                WHEN 'Владеет' THEN 3
                ELSE 4
            END,
            ilc.sort_order,
            ilc.raw_value
        """,
        (profile_name,),
    ).fetchall()

    indicator_map: dict[int, list[tuple[str, str]]] = {}
    seen: dict[int, set[tuple[str, str]]] = {}
    for skill_id, indicator_type, indicator_text in rows:
        seen.setdefault(skill_id, set())
        key = (indicator_type, indicator_text)
        if key in seen[skill_id]:
            continue
        seen[skill_id].add(key)
        indicator_map.setdefault(skill_id, []).append(key)
    return indicator_map


def load_live_snapshot_map() -> dict[str, list[tuple[str, str]]]:
    if not LIVE_SNAPSHOT.exists():
        return {}

    payload = json.loads(LIVE_SNAPSHOT.read_text(encoding="utf-8"))
    live_indicator_map: dict[str, list[tuple[str, str]]] = {}
    for competency in payload.get("competencies", []):
        for skill in competency.get("skills", []):
            parsed_indicators: list[tuple[str, str]] = []
            for raw_indicator in skill.get("indicators", []):
                if ":" in raw_indicator:
                    indicator_type, indicator_text = raw_indicator.split(":", 1)
                    parsed_indicators.append((indicator_type.strip(), indicator_text.strip()))
                else:
                    parsed_indicators.append(("Не указано", raw_indicator.strip()))
            live_indicator_map[skill["name"]] = parsed_indicators
    return live_indicator_map


def load_indicator_complexity_map(
    source_conn: sqlite3.Connection,
    profile_name: str,
) -> tuple[dict[int, dict[tuple[str, str], dict[str, object]]], dict[int, dict[str, dict[str, object]]]]:
    rows = source_conn.execute(
        """
        SELECT
            s.id AS skill_id,
            COALESCE(d.title, 'Не указано') AS indicator_type,
            ilc.raw_value AS indicator_text,
            ps.title AS scale_title,
            pl.sort_order AS complexity_sort_order,
            pl.canonical_band AS complexity_band,
            pl.title AS complexity_label
        FROM profile p
        JOIN profile_competency pc ON pc.profile_id = p.id
        JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        JOIN skill s ON s.id = cs.skill_id
        JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        LEFT JOIN dimension d ON d.id = ir.dimension_id
        JOIN indicator_level_cell ilc ON ilc.indicator_row_id = ir.id
        LEFT JOIN proficiency_level pl ON pl.id = ilc.proficiency_level_id
        LEFT JOIN proficiency_scale ps ON ps.id = pc.scale_id
        WHERE p.name = ?
          AND COALESCE(TRIM(ilc.raw_value), '') <> ''
        """,
        (profile_name,),
    ).fetchall()

    typed_map: dict[int, dict[tuple[str, str], dict[str, object]]] = {}
    text_map: dict[int, dict[str, dict[str, object]]] = {}

    for row in rows:
        if row["complexity_sort_order"] is None:
            continue
        typed_key = (row["indicator_type"], normalize_key(row["indicator_text"]))
        text_key = normalize_key(row["indicator_text"])

        for bucket, key in ((typed_map.setdefault(row["skill_id"], {}), typed_key), (text_map.setdefault(row["skill_id"], {}), text_key)):
            existing = bucket.get(key)
            if existing is None:
                bucket[key] = {
                    "complexity_sort_order": row["complexity_sort_order"],
                    "complexity_band": row["complexity_band"],
                    "min_complexity_label": row["complexity_label"] or complexity_label_for_band(row["complexity_band"]),
                    "complexity_label": row["complexity_label"] or complexity_label_for_band(row["complexity_band"]),
                    "max_complexity_sort_order": row["complexity_sort_order"],
                    "max_complexity_band": row["complexity_band"],
                    "max_complexity_label": row["complexity_label"] or complexity_label_for_band(row["complexity_band"]),
                    "source_scale_title": row["scale_title"],
                }
                continue

            if row["complexity_sort_order"] < existing["complexity_sort_order"]:
                existing["complexity_sort_order"] = row["complexity_sort_order"]
                existing["complexity_band"] = row["complexity_band"]
                existing["min_complexity_label"] = row["complexity_label"] or complexity_label_for_band(row["complexity_band"])
            if row["complexity_sort_order"] > existing["max_complexity_sort_order"]:
                existing["max_complexity_sort_order"] = row["complexity_sort_order"]
                existing["max_complexity_band"] = row["complexity_band"]
                existing["max_complexity_label"] = row["complexity_label"] or complexity_label_for_band(row["complexity_band"])
            if not existing.get("source_scale_title") and row["scale_title"]:
                existing["source_scale_title"] = row["scale_title"]

    for bucket in list(typed_map.values()) + list(text_map.values()):
        for entry in bucket.values():
            entry["complexity_label"] = build_complexity_summary(
                entry.get("complexity_band"),
                entry.get("max_complexity_band"),
                entry.get("min_complexity_label"),
                entry.get("max_complexity_label"),
            )

    return typed_map, text_map


def resolve_indicator_complexity(
    typed_map: dict[int, dict[tuple[str, str], dict[str, object]]],
    text_map: dict[int, dict[str, dict[str, object]]],
    source_skill_id: int,
    indicator_type: str,
    indicator_text: str,
) -> dict[str, object] | None:
    skill_typed = typed_map.get(source_skill_id, {})
    typed_key = (indicator_type, normalize_key(indicator_text))
    if typed_key in skill_typed:
        return skill_typed[typed_key]

    skill_text = text_map.get(source_skill_id, {})
    return skill_text.get(normalize_key(indicator_text))


def copy_review_queue(source_conn: sqlite3.Connection, target_conn: sqlite3.Connection) -> int:
    has_resolution_note = source_has_column(source_conn, "review_queue", "resolution_note")
    has_reviewed_at = source_has_column(source_conn, "review_queue", "reviewed_at")
    has_updated_at = source_has_column(source_conn, "review_queue", "updated_at")

    select_columns = [
        "id",
        "entity_type",
        "entity_id",
        "severity",
        "reason_code",
        "source_ref",
        "details",
        "status",
    ]
    select_columns.append("resolution_note" if has_resolution_note else "NULL AS resolution_note")
    select_columns.append("reviewed_at" if has_reviewed_at else "NULL AS reviewed_at")
    select_columns.append("created_at")
    select_columns.append("updated_at" if has_updated_at else "NULL AS updated_at")

    rows = source_conn.execute(f"SELECT {', '.join(select_columns)} FROM review_queue ORDER BY id").fetchall()
    for row in rows:
        target_conn.execute(
            """
            INSERT INTO review_queue (
                source_review_id,
                entity_type,
                entity_key,
                severity,
                reason_code,
                source_ref,
                details,
                status,
                resolution_note,
                reviewed_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row[0],
                row[1],
                f"{row[1]}:{row[2]}" if row[2] is not None else row[1],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
                row[10],
                row[11],
            ),
        )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build target catalog DB with hierarchy group -> skill -> indicator.")
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB, help="Source SQLite catalog DB.")
    parser.add_argument("--output-db", type=Path, default=DEFAULT_TARGET_DB, help="Output target SQLite DB.")
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY, help="Path to summary JSON.")
    parser.add_argument("--profile-like", default=None, help="Fallback SQL LIKE pattern for source profile selection.")
    args = parser.parse_args()

    schema_path = Path(__file__).resolve().parents[1] / "sql" / "target_catalog_schema.sql"
    output_db = args.output_db.resolve()
    output_db.parent.mkdir(parents=True, exist_ok=True)
    if output_db.exists():
        output_db.unlink()

    source_conn = sqlite3.connect(args.source_db.resolve())
    source_conn.row_factory = sqlite3.Row
    target_conn = sqlite3.connect(output_db)
    target_conn.row_factory = sqlite3.Row

    try:
        target_conn.executescript(schema_path.read_text(encoding="utf-8"))
        profile_name = choose_profile_name(source_conn, args.profile_like)
        indicator_map = load_indicator_map(source_conn, profile_name)
        live_indicator_map = load_live_snapshot_map()
        typed_complexity_map, text_complexity_map = load_indicator_complexity_map(source_conn, profile_name)

        target_conn.execute(
            """
            INSERT INTO import_run (source_db_path, source_profile_name, notes)
            VALUES (?, ?, ?)
            """,
            (str(args.source_db.resolve()), profile_name, "Imported from local normalized catalog"),
        )

        group_rows = source_conn.execute(
            """
            SELECT id, name, sort_order
            FROM typed_competency
            WHERE status = 'active'
            ORDER BY sort_order, name
            """
        ).fetchall()

        group_id_map: dict[int, int] = {}
        for row in group_rows:
            cursor = target_conn.execute(
                """
                INSERT INTO skill_group (code, name, sort_order, status, source)
                VALUES (?, ?, ?, 'active', 'live_snapshot')
                """,
                (f"group-{slugify(row['name'])}", row["name"], row["sort_order"]),
            )
            group_id_map[row["id"]] = cursor.lastrowid

        skill_rows = source_conn.execute(
            """
            SELECT
                tcs.id,
                tcs.typed_competency_id,
                tcs.source_skill_name,
                tcs.sort_order,
                tcs.resolution_status,
                tcs.match_note,
                s.id AS source_skill_id,
                s.canonical_name
            FROM typed_competency_skill tcs
            LEFT JOIN skill s ON s.id = tcs.skill_id
            WHERE tcs.source = 'live_snapshot'
            ORDER BY tcs.typed_competency_id, tcs.sort_order
            """
        ).fetchall()

        target_skill_id_by_source_skill: dict[int, int] = {}
        imported_skill_count = 0
        imported_indicator_count = 0
        alias_count = 0
        live_snapshot_indicator_skills = 0
        source_fallback_indicator_skills = 0
        skills_with_complexity = 0
        indicators_with_complexity = 0

        for row in skill_rows:
            skill_name = row["canonical_name"] or row["source_skill_name"]
            normalized_name = normalize_key(skill_name)
            group_id = group_id_map[row["typed_competency_id"]]
            skill_code = f"{slugify(skill_name)}-{row['id']}"

            cursor = target_conn.execute(
                """
                INSERT INTO skill (
                    group_id,
                    code,
                    name,
                    normalized_name,
                    sort_order,
                    complexity_min_band,
                    complexity_max_band,
                    complexity_summary,
                    source_scale_title,
                    description,
                    source_skill_id,
                    source_skill_name,
                    resolution_status,
                    match_note,
                    is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    group_id,
                    skill_code,
                    skill_name,
                    normalized_name,
                    row["sort_order"],
                    None,
                    None,
                    None,
                    None,
                    None,
                    row["source_skill_id"],
                    row["source_skill_name"],
                    row["resolution_status"],
                    row["match_note"],
                ),
            )
            target_skill_id = cursor.lastrowid
            imported_skill_count += 1
            if row["source_skill_id"] is not None:
                target_skill_id_by_source_skill[row["source_skill_id"]] = target_skill_id

            if row["source_skill_name"] and row["source_skill_name"] != skill_name:
                target_conn.execute(
                    """
                    INSERT INTO skill_alias (skill_id, alias, normalized_alias, source)
                    VALUES (?, ?, ?, 'import')
                    """,
                    (target_skill_id, row["source_skill_name"], normalize_key(row["source_skill_name"])),
                )
                alias_count += 1

            if row["source_skill_id"] is None:
                continue

            effective_indicators = live_indicator_map.get(row["source_skill_name"]) or live_indicator_map.get(skill_name)
            if effective_indicators is not None:
                live_snapshot_indicator_skills += 1
            else:
                effective_indicators = indicator_map.get(row["source_skill_id"], [])
                source_fallback_indicator_skills += 1

            skill_min_order = None
            skill_max_order = None
            skill_min_band = None
            skill_max_band = None
            skill_min_label = None
            skill_max_label = None
            skill_scale_title = None

            for sort_order, (indicator_type, indicator_text) in enumerate(effective_indicators, start=1):
                complexity = resolve_indicator_complexity(
                    typed_complexity_map,
                    text_complexity_map,
                    row["source_skill_id"],
                    indicator_type,
                    indicator_text,
                )
                target_conn.execute(
                    """
                    INSERT INTO indicator (
                        skill_id,
                        indicator_type,
                        text,
                        normalized_text,
                        sort_order,
                        complexity_band,
                        complexity_label,
                        complexity_sort_order,
                        source_indicator_row_id,
                        source_profile_name,
                        source_scale_title,
                        is_active
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        target_skill_id,
                        indicator_type,
                        indicator_text,
                        normalize_key(indicator_text),
                        sort_order,
                        complexity.get("complexity_band") if complexity else None,
                        complexity.get("complexity_label") if complexity else None,
                        complexity.get("complexity_sort_order") if complexity else None,
                        None,
                        profile_name,
                        complexity.get("source_scale_title") if complexity else None,
                    ),
                )
                imported_indicator_count += 1
                if complexity:
                    indicators_with_complexity += 1
                    if skill_min_order is None or complexity["complexity_sort_order"] < skill_min_order:
                        skill_min_order = complexity["complexity_sort_order"]
                        skill_min_band = complexity.get("complexity_band")
                        skill_min_label = complexity.get("min_complexity_label") or complexity_label_for_band(complexity.get("complexity_band"))
                    if skill_max_order is None or complexity["max_complexity_sort_order"] > skill_max_order:
                        skill_max_order = complexity["max_complexity_sort_order"]
                        skill_max_band = complexity.get("max_complexity_band")
                        skill_max_label = complexity.get("max_complexity_label") or complexity.get("complexity_label")
                    if not skill_scale_title and complexity.get("source_scale_title"):
                        skill_scale_title = complexity["source_scale_title"]

            skill_complexity_summary = build_complexity_summary(skill_min_band, skill_max_band, skill_min_label, skill_max_label)
            if skill_complexity_summary:
                target_conn.execute(
                    """
                    UPDATE skill
                    SET complexity_min_band = ?,
                        complexity_max_band = ?,
                        complexity_summary = ?,
                        source_scale_title = ?
                    WHERE id = ?
                    """,
                    (skill_min_band, skill_max_band, skill_complexity_summary, skill_scale_title, target_skill_id),
                )
                skills_with_complexity += 1

        imported_review_count = copy_review_queue(source_conn, target_conn)
        target_conn.commit()

        summary = {
            "profile_name": profile_name,
            "counts": {
                "skill_groups": len(group_rows),
                "skills": imported_skill_count,
                "indicators": imported_indicator_count,
                "skill_aliases": alias_count,
                "review_queue": imported_review_count,
            },
            "indicator_source": {
                "live_snapshot_skills": live_snapshot_indicator_skills,
                "source_fallback_skills": source_fallback_indicator_skills,
            },
            "complexity": {
                "skills_with_complexity": skills_with_complexity,
                "indicators_with_complexity": indicators_with_complexity,
            },
        }
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        source_conn.close()
        target_conn.close()


if __name__ == "__main__":
    main()
