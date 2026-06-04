from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from review_queue_messages import humanize_review_details


DEFAULT_DBS = [
    Path("artifacts/skills_catalog.sqlite"),
]


def table_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(row[1] == column_name for row in conn.execute(f"PRAGMA table_info({table_name})"))


def rewrite_db(db_path: Path) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    has_updated_at = table_has_column(conn, "review_queue", "updated_at")
    has_resolution_note = table_has_column(conn, "review_queue", "resolution_note")
    touched = 0
    cleared_notes = 0
    now = datetime.now(UTC).isoformat()

    rows = conn.execute("SELECT id, reason_code, source_ref, details, resolution_note FROM review_queue ORDER BY id").fetchall()
    for row in rows:
        new_details = humanize_review_details(row["reason_code"], row["source_ref"], row["details"])
        should_clear_note = has_resolution_note and row["resolution_note"] == "smoke test rollback"
        if new_details == row["details"] and not should_clear_note:
            continue

        if has_resolution_note and has_updated_at:
            conn.execute(
                """
                UPDATE review_queue
                SET details = ?,
                    resolution_note = CASE WHEN resolution_note = 'smoke test rollback' THEN NULL ELSE resolution_note END,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_details, now, row["id"]),
            )
        elif has_resolution_note:
            conn.execute(
                """
                UPDATE review_queue
                SET details = ?,
                    resolution_note = CASE WHEN resolution_note = 'smoke test rollback' THEN NULL ELSE resolution_note END
                WHERE id = ?
                """,
                (new_details, row["id"]),
            )
        elif has_updated_at:
            conn.execute(
                """
                UPDATE review_queue
                SET details = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_details, now, row["id"]),
            )
        else:
            conn.execute("UPDATE review_queue SET details = ? WHERE id = ?", (new_details, row["id"]))

        touched += 1
        if should_clear_note:
            cleared_notes += 1

    conn.commit()
    conn.close()
    return touched, cleared_notes


def main() -> None:
    parser = argparse.ArgumentParser(description="Rewrite review_queue messages in a methodologist-friendly language.")
    parser.add_argument("--db", action="append", type=Path, help="Path to a SQLite DB. Can be passed multiple times.")
    args = parser.parse_args()

    db_paths = args.db or DEFAULT_DBS
    for db_path in db_paths:
        touched, cleared_notes = rewrite_db(db_path)
        print(f"{db_path}: updated={touched}, cleared_notes={cleared_notes}")


if __name__ == "__main__":
    main()
