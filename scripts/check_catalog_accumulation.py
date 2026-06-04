from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

os.environ["USE_LIVE"] = "0"
os.environ["USE_COUNCIL"] = "0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from viewer.app import apply_candidate_decision, load_brief_text_from_path, open_db, run_intake_pipeline


def summarize_candidates(result: dict[str, object]) -> dict[str, int]:
    counters = {
        "atomic_skills": 0,
        "matched": 0,
        "alias": 0,
        "fuzzy": 0,
        "new": 0,
        "accepted": 0,
        "needs_review": 0,
        "rejected": 0,
    }
    for item in result.get("candidates", []):
        if not isinstance(item, dict):
            continue
        if item.get("entity_type") != "skill" or item.get("atomicity") != "atomic":
            continue
        counters["atomic_skills"] += 1
        resolution = str(item.get("resolution") or "")
        decision = str(item.get("decision") or "")
        if resolution in counters:
            counters[resolution] += 1
        if decision in counters:
            counters[decision] += 1
    return counters


def accept_open_atomic_suggestions(conn: sqlite3.Connection, brief_id: int) -> int:
    rows = conn.execute(
        """
        SELECT id
        FROM skill_suggestion
        WHERE brief_id = ?
          AND entity_type = 'skill'
          AND atomicity = 'atomic'
          AND decision = 'needs_review'
        ORDER BY id
        """,
        (brief_id,),
    ).fetchall()
    for row in rows:
        apply_candidate_decision(conn, int(row["id"]), "accepted", "accepted by accumulation check")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the same real brief twice on a DB copy and verify catalog accumulation.")
    parser.add_argument("--brief-file", type=Path, required=True, help="Path to .txt/.md/.csv/.docx brief.")
    parser.add_argument("--source-db", type=Path, default=Path("artifacts/skills_catalog.sqlite"))
    parser.add_argument("--work-db", type=Path, default=Path("test_runtime/catalog_accumulation_check.sqlite"))
    parser.add_argument("--json-out", type=Path, default=Path("test_runtime/catalog_accumulation_check.json"))
    args = parser.parse_args()

    source_db = args.source_db.resolve()
    work_db = args.work_db.resolve()
    brief_file = args.brief_file.resolve()
    json_out = args.json_out.resolve()
    work_db.parent.mkdir(parents=True, exist_ok=True)
    json_out.parent.mkdir(parents=True, exist_ok=True)

    if not source_db.exists():
        raise FileNotFoundError(source_db)
    if work_db.exists():
        work_db.unlink()
    shutil.copy2(source_db, work_db)

    brief_text, source_name = load_brief_text_from_path(str(brief_file))
    conn = open_db(work_db)
    try:
        first = run_intake_pipeline(conn, work_db, brief_text, intake_job_id=None)
        first_summary = summarize_candidates(first)
        accepted_from_review = accept_open_atomic_suggestions(conn, int(first["brief_id"]))

        second = run_intake_pipeline(conn, work_db, brief_text, intake_job_id=None)
        second_summary = summarize_candidates(second)
    finally:
        conn.close()

    success = (
        first_summary["atomic_skills"] > 0
        and accepted_from_review > 0
        and second_summary["new"] < first_summary["new"]
        and (second_summary["matched"] + second_summary["alias"]) >= accepted_from_review
    )
    report = {
        "source_name": source_name,
        "work_db": str(work_db),
        "first_brief_id": first["brief_id"],
        "second_brief_id": second["brief_id"],
        "accepted_from_review": accepted_from_review,
        "first": first_summary,
        "second": second_summary,
        "success": success,
    }
    json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
