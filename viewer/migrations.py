from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class MigrationResult:
    migration_id: str
    checksum: str
    applied: bool
    applied_at: str


def _checksum_sql(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            id INTEGER PRIMARY KEY,
            migration_id TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('applied','failed')),
            applied_at TEXT NOT NULL,
            error_text TEXT
        )
        """
    )


def apply_sql_migration(conn: sqlite3.Connection, migration_id: str, sql_path: Path) -> MigrationResult:
    """Apply idempotent SQL and record a reproducible migration ledger entry."""
    sql = sql_path.read_text(encoding="utf-8")
    checksum = _checksum_sql(sql)
    applied_at = datetime.now(UTC).isoformat()
    ensure_migration_table(conn)
    try:
        conn.executescript(sql)
        conn.execute(
            """
            INSERT INTO schema_migration (migration_id, checksum, status, applied_at, error_text)
            VALUES (?, ?, 'applied', ?, NULL)
            ON CONFLICT(migration_id) DO UPDATE SET
                checksum = excluded.checksum,
                status = 'applied',
                applied_at = excluded.applied_at,
                error_text = NULL
            """,
            (migration_id, checksum, applied_at),
        )
        return MigrationResult(migration_id=migration_id, checksum=checksum, applied=True, applied_at=applied_at)
    except Exception as exc:
        conn.execute(
            """
            INSERT INTO schema_migration (migration_id, checksum, status, applied_at, error_text)
            VALUES (?, ?, 'failed', ?, ?)
            ON CONFLICT(migration_id) DO UPDATE SET
                checksum = excluded.checksum,
                status = 'failed',
                applied_at = excluded.applied_at,
                error_text = excluded.error_text
            """,
            (migration_id, checksum, applied_at, str(exc)),
        )
        raise


def _review_queue_allows_prerequisite_edge(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review_queue'"
    ).fetchone()
    if not row:
        return False
    create_sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
    return "prerequisite_edge" in str(create_sql or "")


def migrate_review_queue_entity_types(conn: sqlite3.Connection) -> None:
    """Rebuild review_queue when an old CHECK constraint blocks prerequisite edge reviews."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_queue'").fetchone():
        return
    if _review_queue_allows_prerequisite_edge(conn):
        return

    conn.execute("DROP TABLE IF EXISTS review_queue__old_constraint")
    conn.execute("ALTER TABLE review_queue RENAME TO review_queue__old_constraint")
    conn.execute(
        """
        CREATE TABLE review_queue (
            id INTEGER PRIMARY KEY,
            entity_type TEXT NOT NULL CHECK (
                entity_type IN (
                    'workbook', 'sheet', 'block', 'competency', 'skill',
                    'indicator_row', 'profile', 'project', 'project_indicator',
                    'ai_analysis_run', 'prerequisite_edge'
                )
            ),
            entity_id INTEGER,
            source_ref TEXT,
            reason_code TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
            details TEXT,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
            resolution_note TEXT,
            reviewed_at TEXT,
            updated_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT INTO review_queue(
            id, entity_type, entity_id, source_ref, reason_code, severity,
            details, status, resolution_note, reviewed_at, updated_at, created_at
        )
        SELECT
            id, entity_type, entity_id, source_ref, reason_code, severity,
            details, status, resolution_note, reviewed_at, updated_at, created_at
        FROM review_queue__old_constraint
        """
    )
    conn.execute("DROP TABLE review_queue__old_constraint")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_queue_source_ref ON review_queue(source_ref, status)")


def apply_runtime_migrations(conn: sqlite3.Connection, schema_sql_path: Path) -> list[MigrationResult]:
    """Runtime migrations are intentionally explicit instead of hidden bootstrap SQL."""
    results = [apply_sql_migration(conn, "intake_runtime_schema", schema_sql_path)]
    migrate_review_queue_entity_types(conn)
    return results
