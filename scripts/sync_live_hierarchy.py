from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path

import requests


DEFAULT_DB = Path("artifacts/skills_catalog.sqlite")
DEFAULT_SNAPSHOT = Path("artifacts/live_catalog_snapshot.json")
DEFAULT_REPORT = Path("artifacts/live_hierarchy_sync_report.json")

MANUAL_SKILL_OVERRIDES = {
    "Настройка Java-окружения": "Настройка Java- окружения",
    "Обеспечение безопасности микросервисов": "Обеспечение безопасности микросвервисов",
    "Работа с аннотацией и рефлексией": "Работа с аннотацией и рефлесией",
}


@dataclass
class LiveSkill:
    name: str
    sort_order: int
    indicators: list[str]


@dataclass
class LiveTypedCompetency:
    name: str
    sort_order: int
    skills: list[LiveSkill]


def clean_html_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_lookup(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"[\s\-_\"'`().,/]+", "", value)
    return value


def fetch_live_hierarchy(base_url: str, username: str, password: str) -> list[LiveTypedCompetency]:
    session = requests.Session()
    session.trust_env = False

    login_url = f"{base_url.rstrip('/')}/login.php"
    catalog_url = f"{base_url.rstrip('/')}/competencies.php"

    session.post(
        login_url,
        data={"username": username, "password": password},
        allow_redirects=True,
        timeout=30,
    )
    page_response = session.get(catalog_url, timeout=30)
    if page_response.status_code != 200:
        raise RuntimeError(f"Live catalog request failed with {page_response.status_code}")

    html = page_response.text
    comp_starts = list(re.finditer(r'<div class="competency" id="competency-\d+">', html))
    hierarchy: list[LiveTypedCompetency] = []

    for comp_index, comp_match in enumerate(comp_starts, start=1):
        comp_start = comp_match.start()
        comp_end = comp_starts[comp_index].start() if comp_index < len(comp_starts) else html.find("<script", comp_start)
        comp_block = html[comp_start:comp_end]

        comp_name_match = re.search(r'<span class="competency-name">(.*?)</span>', comp_block, re.S)
        if not comp_name_match:
            continue
        comp_name = clean_html_text(comp_name_match.group(1))

        skill_starts = list(re.finditer(r'<div class="skill" id="skill-\d+">', comp_block))
        skills: list[LiveSkill] = []
        for skill_index, skill_match in enumerate(skill_starts, start=1):
            skill_start = skill_match.start()
            skill_end = skill_starts[skill_index].start() if skill_index < len(skill_starts) else len(comp_block)
            skill_block = comp_block[skill_start:skill_end]

            skill_name_match = re.search(r'<span class="skill-name">(.*?)</span>', skill_block, re.S)
            if not skill_name_match:
                continue

            indicators = [
                f"{clean_html_text(kind)} {clean_html_text(text)}".strip()
                for kind, text in re.findall(
                    r'<div class="indicator-type [^"]+">\s*(.*?)\s*</div>\s*<div class="indicator-text">\s*(.*?)\s*</div>',
                    skill_block,
                    re.S,
                )
            ]
            skills.append(
                LiveSkill(
                    name=clean_html_text(skill_name_match.group(1)),
                    sort_order=skill_index,
                    indicators=indicators,
                )
            )

        hierarchy.append(LiveTypedCompetency(name=comp_name, sort_order=comp_index, skills=skills))

    if not hierarchy:
        raise RuntimeError("Live hierarchy parsing returned no competencies.")
    return hierarchy


def ensure_hierarchy_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS typed_competency (
            id INTEGER PRIMARY KEY,
            normalized_name TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'live_snapshot', 'derived')),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'candidate', 'deprecated'))
        );

        CREATE TABLE IF NOT EXISTS typed_competency_skill (
            id INTEGER PRIMARY KEY,
            typed_competency_id INTEGER NOT NULL REFERENCES typed_competency(id) ON DELETE CASCADE,
            skill_id INTEGER REFERENCES skill(id) ON DELETE SET NULL,
            source_skill_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            resolution_status TEXT NOT NULL DEFAULT 'matched' CHECK (resolution_status IN ('matched', 'alias', 'manual', 'fuzzy', 'missing')),
            match_note TEXT,
            source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'live_snapshot', 'derived')),
            UNIQUE (typed_competency_id, source_skill_name)
        );

        CREATE INDEX IF NOT EXISTS idx_typed_competency_order ON typed_competency (sort_order, name);
        CREATE INDEX IF NOT EXISTS idx_typed_competency_skill_typed_competency ON typed_competency_skill (typed_competency_id, sort_order);
        CREATE INDEX IF NOT EXISTS idx_typed_competency_skill_skill ON typed_competency_skill (skill_id, resolution_status);
        """
    )


def load_skill_maps(conn: sqlite3.Connection) -> tuple[dict[str, sqlite3.Row], dict[str, list[sqlite3.Row]]]:
    conn.row_factory = sqlite3.Row
    skills = conn.execute(
        """
        SELECT id, canonical_name, normalized_name
        FROM skill
        ORDER BY canonical_name
        """
    ).fetchall()
    exact_map = {row["canonical_name"]: row for row in skills}
    normalized_map: dict[str, list[sqlite3.Row]] = {}
    for row in skills:
        normalized_map.setdefault(normalize_lookup(row["canonical_name"]), []).append(row)

    alias_rows = conn.execute(
        """
        SELECT sa.alias, sa.normalized_alias, s.id, s.canonical_name
        FROM skill_alias sa
        JOIN skill s ON s.id = sa.skill_id
        """
    ).fetchall()
    for row in alias_rows:
        normalized_map.setdefault(normalize_lookup(row["alias"]), []).append(row)
        exact_map.setdefault(row["alias"], row)

    return exact_map, normalized_map


def resolve_skill(
    live_skill_name: str,
    exact_map: dict[str, sqlite3.Row],
    normalized_map: dict[str, list[sqlite3.Row]],
) -> tuple[int | None, str, str | None]:
    direct = exact_map.get(live_skill_name)
    if direct is not None:
        return direct["id"], "matched", None

    override_name = MANUAL_SKILL_OVERRIDES.get(live_skill_name)
    if override_name and override_name in exact_map:
        return exact_map[override_name]["id"], "manual", f"manual override -> {override_name}"

    normalized = normalize_lookup(live_skill_name)
    candidates = normalized_map.get(normalized, [])
    unique_candidates = {candidate["id"]: candidate for candidate in candidates}
    if len(unique_candidates) == 1:
        candidate = next(iter(unique_candidates.values()))
        return candidate["id"], "alias", f"normalized alias -> {candidate['canonical_name']}"

    best_ratio = 0.0
    best_match: sqlite3.Row | None = None
    for rows in normalized_map.values():
        for candidate in rows:
            ratio = SequenceMatcher(None, normalized, normalize_lookup(candidate["canonical_name"])).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = candidate
    if best_match is not None and best_ratio >= 0.92:
        return best_match["id"], "fuzzy", f"fuzzy {best_ratio:.3f} -> {best_match['canonical_name']}"

    return None, "missing", None


def ensure_alias(conn: sqlite3.Connection, skill_id: int, alias: str) -> None:
    normalized_alias = normalize_lookup(alias)
    conn.execute(
        """
        INSERT OR IGNORE INTO skill_alias (skill_id, alias, normalized_alias, source)
        VALUES (?, ?, ?, ?)
        """,
        (skill_id, alias, normalized_alias, "live_hierarchy_sync"),
    )


def sync_hierarchy(
    conn: sqlite3.Connection,
    hierarchy: list[LiveTypedCompetency],
) -> dict[str, object]:
    ensure_hierarchy_schema(conn)
    exact_map, normalized_map = load_skill_maps(conn)

    conn.execute("DELETE FROM typed_competency_skill WHERE source = 'live_snapshot'")
    conn.execute("DELETE FROM typed_competency WHERE source = 'live_snapshot'")

    report = {
        "typed_competency_count": len(hierarchy),
        "skill_count": sum(len(item.skills) for item in hierarchy),
        "resolution_counts": {
            "matched": 0,
            "alias": 0,
            "manual": 0,
            "fuzzy": 0,
            "missing": 0,
        },
        "missing_skills": [],
        "non_exact_matches": [],
    }

    for typed_competency in hierarchy:
        cursor = conn.execute(
            """
            INSERT INTO typed_competency (normalized_name, name, sort_order, source, status)
            VALUES (?, ?, ?, 'live_snapshot', 'active')
            """,
            (normalize_lookup(typed_competency.name), typed_competency.name, typed_competency.sort_order),
        )
        typed_competency_id = cursor.lastrowid

        for live_skill in typed_competency.skills:
            skill_id, resolution_status, match_note = resolve_skill(live_skill.name, exact_map, normalized_map)
            report["resolution_counts"][resolution_status] += 1
            if resolution_status == "missing":
                report["missing_skills"].append(live_skill.name)
            elif resolution_status != "matched":
                report["non_exact_matches"].append(
                    {
                        "live_skill_name": live_skill.name,
                        "resolution_status": resolution_status,
                        "match_note": match_note,
                    }
                )

            conn.execute(
                """
                INSERT INTO typed_competency_skill (
                    typed_competency_id,
                    skill_id,
                    source_skill_name,
                    sort_order,
                    resolution_status,
                    match_note,
                    source
                )
                VALUES (?, ?, ?, ?, ?, ?, 'live_snapshot')
                """,
                (
                    typed_competency_id,
                    skill_id,
                    live_skill.name,
                    live_skill.sort_order,
                    resolution_status,
                    match_note,
                ),
            )

            if skill_id is not None:
                local_name = conn.execute("SELECT canonical_name FROM skill WHERE id = ?", (skill_id,)).fetchone()[0]
                if local_name != live_skill.name:
                    ensure_alias(conn, skill_id, live_skill.name)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync the live competency hierarchy into the local SQLite database.")
    parser.add_argument("--base-url", default="https://arturincloud.kz", help="Base URL of the live site.")
    parser.add_argument("--username", required=True, help="Login username for the live site.")
    parser.add_argument("--password", required=True, help="Login password for the live site.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to the local SQLite database.")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT, help="Where to write the raw live snapshot.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Where to write the sync report.")
    args = parser.parse_args()

    hierarchy = fetch_live_hierarchy(args.base_url, args.username, args.password)
    args.snapshot.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    try:
        report = sync_hierarchy(conn, hierarchy)
        conn.commit()
    finally:
        conn.close()

    snapshot_payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "competencies": [asdict(item) for item in hierarchy],
    }
    args.snapshot.write_text(json.dumps(snapshot_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
