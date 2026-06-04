from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from methodologist_review_resolution import (
    apply_methodologist_review_decisions,
    update_summary_open_reviews,
)


DEFAULT_SOURCE_DB = Path("artifacts/skills_catalog.sqlite")
DEFAULT_SUMMARY_JSON = Path("artifacts/catalog_summary.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply methodologist decisions to review_queue and supporting source data.")
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY_JSON)
    args = parser.parse_args()

    source_db = args.source_db.resolve()
    summary_json = args.summary_json.resolve()

    conn = sqlite3.connect(source_db)
    try:
        metrics = apply_methodologist_review_decisions(conn)
        update_summary_open_reviews(conn, summary_json)
    finally:
        conn.close()

    print(json.dumps({"source_metrics": metrics}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
