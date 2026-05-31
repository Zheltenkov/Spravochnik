from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from html import unescape
from pathlib import Path

import requests


DEFAULT_DB = Path("artifacts/skills_catalog.sqlite")
DEFAULT_OUTPUT = Path("artifacts/live_catalog_comparison.json")


def clean_html_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


@dataclass
class LiveSkill:
    competency: str
    indicators: list[str]


def fetch_live_catalog(base_url: str, username: str, password: str) -> dict[str, LiveSkill]:
    session = requests.Session()
    session.trust_env = False

    login_url = f"{base_url.rstrip('/')}/login.php"
    catalog_url = f"{base_url.rstrip('/')}/competencies.php"

    login_response = session.post(
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
    live: dict[str, LiveSkill] = {}

    for index, match in enumerate(comp_starts):
        start = match.start()
        end = comp_starts[index + 1].start() if index + 1 < len(comp_starts) else html.find("<script", start)
        block = html[start:end]
        comp_name_match = re.search(r'<span class="competency-name">(.*?)</span>', block, re.S)
        comp_name = clean_html_text(comp_name_match.group(1)) if comp_name_match else "[unknown]"

        skill_starts = list(re.finditer(r'<div class="skill" id="skill-\d+">', block))
        for skill_index, skill_match in enumerate(skill_starts):
            skill_start = skill_match.start()
            skill_end = skill_starts[skill_index + 1].start() if skill_index + 1 < len(skill_starts) else len(block)
            skill_block = block[skill_start:skill_end]

            skill_name_match = re.search(r'<span class="skill-name">(.*?)</span>', skill_block, re.S)
            if not skill_name_match:
                continue

            skill_name = clean_html_text(skill_name_match.group(1))
            indicators = [
                f"{clean_html_text(kind)} {clean_html_text(text)}".strip()
                for kind, text in re.findall(
                    r'<div class="indicator-type [^"]+">\s*(.*?)\s*</div>\s*<div class="indicator-text">\s*(.*?)\s*</div>',
                    skill_block,
                    re.S,
                )
            ]
            live[skill_name] = LiveSkill(competency=comp_name, indicators=indicators)

    if not live:
        raise RuntimeError(
            f"Live catalog parsing returned no skills. Login URL ended at {login_response.url!r}, "
            f"catalog URL ended at {page_response.url!r}."
        )
    return live


def fetch_local_profile_catalog(db_path: Path, profile_name_pattern: str) -> tuple[str, dict[str, set[str]]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        profile = conn.execute(
            "select id, name from profile where name like ? order by id limit 1",
            (profile_name_pattern,),
        ).fetchone()
        if not profile:
            raise RuntimeError(f"No profile matched pattern {profile_name_pattern!r}")

        rows = conn.execute(
            """
            select
                s.canonical_name as skill_name,
                coalesce(d.title, '') as dimension,
                ilc.raw_value as indicator_value
            from profile p
            join profile_competency pc on pc.profile_id = p.id
            join competency_skill cs on cs.profile_competency_id = pc.id
            join skill s on s.id = cs.skill_id
            left join indicator_row ir on ir.competency_skill_id = cs.id
            left join dimension d on d.id = ir.dimension_id
            left join indicator_level_cell ilc on ilc.indicator_row_id = ir.id
            where p.id = ?
            """,
            (profile["id"],),
        ).fetchall()

        local: dict[str, set[str]] = {}
        for row in rows:
            skill_name = row["skill_name"]
            local.setdefault(skill_name, set())
            if row["indicator_value"]:
                prefix = f"{row['dimension']}:" if row["dimension"] else ""
                local[skill_name].add(f"{prefix} {row['indicator_value']}".strip())
        return profile["name"], local
    finally:
        conn.close()


def compare_catalogs(live: dict[str, LiveSkill], local: dict[str, set[str]]) -> dict[str, object]:
    live_skills = set(live)
    local_skills = set(local)
    matched = sorted(live_skills & local_skills)
    missing_from_local = sorted(live_skills - local_skills)
    extra_in_local = sorted(local_skills - live_skills)

    exact_indicator_matches: list[str] = []
    indicator_mismatches: list[dict[str, object]] = []

    for skill in matched:
        live_set = set(live[skill].indicators)
        local_set = local[skill]
        if live_set == local_set:
            exact_indicator_matches.append(skill)
            continue

        indicator_mismatches.append(
            {
                "skill": skill,
                "live_count": len(live_set),
                "local_count": len(local_set),
                "missing_in_local": sorted(live_set - local_set)[:10],
                "extra_in_local": sorted(local_set - live_set)[:10],
            }
        )

    live_competencies = sorted({item.competency for item in live.values()})
    return {
        "live_competency_count": len(live_competencies),
        "live_competency_names": live_competencies,
        "live_skill_count": len(live_skills),
        "local_skill_count": len(local_skills),
        "matched_skill_names": len(matched),
        "missing_from_local": missing_from_local,
        "extra_in_local": extra_in_local,
        "exact_indicator_match_count": len(exact_indicator_matches),
        "indicator_mismatch_count": len(indicator_mismatches),
        "indicator_mismatch_examples": indicator_mismatches[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare live arturincloud catalog with local SQLite import.")
    parser.add_argument("--base-url", default="https://arturincloud.kz", help="Base URL of the live site.")
    parser.add_argument("--username", required=True, help="Login username for the live site.")
    parser.add_argument("--password", required=True, help="Login password for the live site.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to local SQLite database.")
    parser.add_argument(
        "--profile-like",
        default="%Java%",
        help="SQL LIKE pattern used to pick the local profile that should match the live catalog.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Where to write JSON comparison summary.")
    args = parser.parse_args()

    live = fetch_live_catalog(args.base_url, args.username, args.password)
    profile_name, local = fetch_local_profile_catalog(args.db, args.profile_like)
    report = compare_catalogs(live, local)
    report["profile_name"] = profile_name

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
