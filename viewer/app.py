from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
from math import isfinite
import mimetypes
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode
from wsgiref.simple_server import make_server
import xml.etree.ElementTree as ET

from jinja2 import Environment, FileSystemLoader, select_autoescape


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
PROJECT_ROOT = BASE_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_DB = BASE_DIR.parent / "artifacts" / "skills_catalog.sqlite"
DEFAULT_TARGET_DB = BASE_DIR.parent / "artifacts" / "target_catalog.sqlite"
DEFAULT_SUMMARY = BASE_DIR.parent / "artifacts" / "catalog_summary.json"
DEFAULT_COMPARE_REPORT = BASE_DIR.parent / "artifacts" / "live_catalog_comparison.json"
INTAKE_SCHEMA_SQL = BASE_DIR.parent / "spravochnik_intake" / "sql" / "new_tables.sql"
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
TARGET_SCHEMA_READY: set[str] = set()
INTAKE_SCHEMA_READY: set[str] = set()
INTAKE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="intake")
ACTIVE_INTAKE_JOB_IDS: set[int] = set()
INTAKE_STALE_TIMEOUT_SECONDS = 180

COMPLEXITY_OPTIONS = [
    ("", "Не указано"),
    ("trainee", "Стажер"),
    ("junior_minus", "Начальный (junior-)"),
    ("junior", "Начальный (junior)"),
    ("basic", "Базовый"),
    ("junior_plus", "Базовый (junior+)"),
    ("middle", "Продвинутый (middle)"),
    ("senior", "Продвинутый (senior)"),
    ("master", "Мастерский"),
]
COMPLEXITY_LABELS = {value: label for value, label in COMPLEXITY_OPTIONS if value}
COMPLEXITY_ORDER = {value: index for index, (value, _label) in enumerate(COMPLEXITY_OPTIONS) if value}
REVIEW_REASON_LABELS = {
    "missing_dimension": "Не указан тип индикатора",
    "missing_block_title": "У блока нет названия",
    "orphan_indicator_row": "Строка не привязана к skill",
    "ambiguous_skill_name": "Нужно уточнить название skill",
    "no_header_rows": "Не найден заголовок блока",
    "ambiguous_block_title": "Нужно уточнить название блока",
    "level_headers_inherited_from_previous_block": "Шкала унаследована от предыдущего блока",
    "skill_name_trimmed": "Название skill было очищено",
    "base_text_without_levels": "Есть текст индикатора без уровней",
    "novel_skill": "Новый skill не найден в каталоге",
    "fuzzy_match_ambiguous": "Нечеткое совпадение с каталогом",
    "low_confidence": "Низкая уверенность модели",
    "single_source": "Недостаточно подтверждающих источников",
    "council_split": "Модели не согласились между собой",
    "auto_accept_policy": "Автопринято по policy: уверенность >= 0.95 и согласие жюри = 1.00",
    "composite_decomposed": "Кандидат разбит на атомарные части",
    "non_skill:competency_block": "Это блок программы, а не skill",
    "non_skill:curriculum_section": "Это учебный раздел, а не skill",
    "program_brief_publication_guardrail": "Новый skill из program brief требует методологического подтверждения",
    "needs_review": "Нужна методологическая проверка",
    "cycle_broken": "Цикл в графе был разорван",
    "redundant_transitive": "Ребро признано транзитивно избыточным",
    "bloom_direction": "Нарушено направление по Блуму",
    "ai_proposed": "Ребро предложено AI и требует проверки",
    "low_confidence": "Низкая уверенность ребра",
}
REVIEW_STATUS_LABELS = {
    "open": "Открыто",
    "resolved": "Решено",
    "ignored": "Пропущено",
    "all": "Все",
}
REVIEW_SEVERITY_LABELS = {
    "error": "Ошибка",
    "warning": "Внимание",
    "info": "Инфо",
    "all": "Все",
}
INTAKE_JOB_STATUS_LABELS = {
    "pending": "В очереди",
    "running": "Обрабатывается",
    "succeeded": "Готово",
    "failed": "Ошибка",
}
INTAKE_STAGE_LABELS = {
    "queued": "Постановка в очередь",
    "starting": "Запуск",
    "decompose": "Декомпозиция брифа",
    "search": "Поиск evidence",
    "synthesize": "Синтез навыков",
    "atomize": "Атомизация кандидатов",
    "normalize": "Нормализация и дедупликация",
    "resolve": "Резолв против каталога",
    "council": "Экспертное жюри",
    "triage": "Финальный триаж",
    "prerequisites": "Пререквизиты",
    "persist": "Запись в БД",
    "plan": "Черновик УП",
    "completed": "Завершено",
    "failed": "Ошибка",
}
INTAKE_PROGRESS_STEPS = [
    {"code": "queued", "label": "Очередь"},
    {"code": "decompose", "label": "Декомпозиция"},
    {"code": "search", "label": "Поиск"},
    {"code": "normalize", "label": "Нормализация"},
    {"code": "resolve", "label": "Резолв"},
    {"code": "council", "label": "Council"},
    {"code": "persist", "label": "Запись"},
    {"code": "plan", "label": "УП"},
    {"code": "completed", "label": "Готово"},
]


def normalize_search_text(value: object | None) -> str:
    if value is None:
        return ""
    return " ".join(str(value).casefold().replace("ё", "е").split())


def review_reason_label(reason_code: str | None) -> str:
    if not reason_code:
        return "Нужна проверка"
    return REVIEW_REASON_LABELS.get(reason_code, reason_code.replace("_", " "))


def review_status_label(status: str | None) -> str:
    if not status:
        return "Не указан"
    return REVIEW_STATUS_LABELS.get(status, status)


def review_severity_label(severity: str | None) -> str:
    if not severity:
        return "Не указано"
    return REVIEW_SEVERITY_LABELS.get(severity, severity)


def intake_job_status_label(status: str | None) -> str:
    if not status:
        return "Неизвестно"
    return INTAKE_JOB_STATUS_LABELS.get(status, status)


def intake_stage_label(stage: str | None) -> str:
    if not stage:
        return "Не указан"
    return INTAKE_STAGE_LABELS.get(stage, stage)


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_function("search_norm", 1, normalize_search_text)
    ensure_runtime_schema(conn)
    return conn


def open_target_db(db_path: Path) -> sqlite3.Connection:
    resolved = str(Path(db_path).resolve())
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_function("search_norm", 1, normalize_search_text)
    if resolved not in TARGET_SCHEMA_READY:
        ensure_target_runtime_schema(conn)
        TARGET_SCHEMA_READY.add(resolved)
    return conn


def load_summary(summary_path: Path) -> dict[str, object]:
    if not summary_path.exists():
        return {}
    return json.loads(summary_path.read_text(encoding="utf-8"))


def fetch_one(conn: sqlite3.Connection, query: str, params: tuple = ()) -> dict[str, object] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def fetch_all(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict[str, object]]:
    return [dict(row) for row in conn.execute(query, params)]


@dataclass
class UploadedFile:
    filename: str
    content_type: str
    data: bytes


def _read_request_body(environ) -> bytes:
    content_length = int(environ.get("CONTENT_LENGTH") or 0)
    if content_length <= 0:
        return b""
    return environ["wsgi.input"].read(content_length)


def parse_multipart_form_data(raw_body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, UploadedFile]]:
    header_blob = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=email_policy).parsebytes(header_blob + raw_body)
    form_data: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}

    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        field_name = part.get_param("name", header="content-disposition")
        if not field_name:
            continue

        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            if payload:
                files[field_name] = UploadedFile(
                    filename=filename,
                    content_type=part.get_content_type(),
                    data=payload,
                )
            continue

        charset = part.get_content_charset() or "utf-8"
        form_data[field_name] = payload.decode(charset, errors="replace")

    return form_data, files


def parse_post_form_and_files(environ) -> tuple[dict[str, str], dict[str, UploadedFile]]:
    raw_body = _read_request_body(environ)
    if not raw_body:
        return {}, {}

    content_type = environ.get("CONTENT_TYPE", "")
    if content_type.casefold().startswith("multipart/form-data"):
        return parse_multipart_form_data(raw_body, content_type)

    parsed = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}, {}


def parse_post_data(environ) -> dict[str, str]:
    form_data, _files = parse_post_form_and_files(environ)
    return form_data


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(row["name"] == column_name for row in conn.execute(f"PRAGMA table_info({table_name})"))


def ensure_runtime_schema(conn: sqlite3.Connection) -> None:
    review_columns = {
        "resolution_note": "TEXT",
        "reviewed_at": "TEXT",
        "updated_at": "TEXT",
    }
    if table_exists(conn, "review_queue"):
        for column_name, column_type in review_columns.items():
            if not column_exists(conn, "review_queue", column_name):
                conn.execute(f"ALTER TABLE review_queue ADD COLUMN {column_name} {column_type}")
        conn.commit()


def ensure_intake_runtime_schema(conn: sqlite3.Connection, db_path: Path) -> None:
    resolved = str(db_path.resolve())
    schema_ready = (
        table_exists(conn, "profile_brief")
        and table_exists(conn, "curriculum_plan")
        and table_exists(conn, "curriculum_plan_row")
        and column_exists(conn, "skill_suggestion", "coverage_area")
        and column_exists(conn, "skill_suggestion", "indicators_json")
    )
    if resolved not in INTAKE_SCHEMA_READY or not schema_ready:
        from spravochnik_intake.pipeline import storage as intake_storage

        intake_storage.apply_migration(conn, str(INTAKE_SCHEMA_SQL))
        repair_intake_review_links(conn)
        INTAKE_SCHEMA_READY.add(resolved)
    repair_stale_intake_jobs(conn)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso_datetime(value: object | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def repair_stale_intake_jobs(conn: sqlite3.Connection, stale_after_seconds: int = INTAKE_STALE_TIMEOUT_SECONDS) -> int:
    if not table_exists(conn, "intake_job"):
        return 0

    now = datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT id, status, current_stage, updated_at, started_at
        FROM intake_job
        WHERE status IN ('pending', 'running')
        """
    ).fetchall()

    stale_ids: list[int] = []
    for row in rows:
        job_id = int(row["id"])
        if job_id in ACTIVE_INTAKE_JOB_IDS:
            continue
        pivot = parse_iso_datetime(row["updated_at"]) or parse_iso_datetime(row["started_at"])
        if pivot is None:
            stale_ids.append(job_id)
            continue
        age_seconds = (now - pivot).total_seconds()
        if age_seconds >= stale_after_seconds:
            stale_ids.append(job_id)

    if not stale_ids:
        return 0

    finished_at = utc_now_iso()
    conn.executemany(
        """
        UPDATE intake_job
        SET status = 'failed',
            current_stage = 'failed',
            progress_note = 'Обработка была прервана: активный worker не найден.',
            error_text = 'Фоновая задача была потеряна после перезапуска приложения или сбоя worker-процесса.',
            updated_at = ?,
            finished_at = ?
        WHERE id = ?
        """,
        [(finished_at, finished_at, job_id) for job_id in stale_ids],
    )
    conn.commit()
    return len(stale_ids)


def create_intake_job(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    source_name: str | None,
    file_path: str | None,
    brief_text: str,
    use_council: bool,
) -> int:
    current_time = utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO intake_job(
            source_kind,
            source_name,
            file_path,
            brief_text,
            status,
            current_stage,
            progress_note,
            use_council,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 'pending', 'queued', 'Задача поставлена в очередь на обработку.', ?, ?, ?)
        """,
        (source_kind, source_name, file_path, brief_text, 1 if use_council else 0, current_time, current_time),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_intake_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str | None = None,
    current_stage: str | None = None,
    progress_note: str | None = None,
    error_text: str | None = None,
    result_payload: dict[str, object] | None = None,
    mark_started: bool = False,
    mark_finished: bool = False,
) -> None:
    fields: list[str] = ["updated_at = ?"]
    params: list[object] = [utc_now_iso()]

    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if current_stage is not None:
        fields.append("current_stage = ?")
        params.append(current_stage)
    if progress_note is not None:
        fields.append("progress_note = ?")
        params.append(progress_note)
    if error_text is not None:
        fields.append("error_text = ?")
        params.append(error_text)
    if result_payload is not None:
        fields.append("result_payload = ?")
        params.append(json.dumps(result_payload, ensure_ascii=False))
    if mark_started:
        fields.append("started_at = ?")
        params.append(utc_now_iso())
    if mark_finished:
        fields.append("finished_at = ?")
        params.append(utc_now_iso())

    params.append(job_id)
    conn.execute(f"UPDATE intake_job SET {', '.join(fields)} WHERE id = ?", tuple(params))
    conn.commit()


def get_intake_job(conn: sqlite3.Connection, job_id: int) -> dict[str, object] | None:
    row = conn.execute("SELECT * FROM intake_job WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    job = dict(row)
    if job.get("result_payload"):
        try:
            job["result_payload"] = json.loads(job["result_payload"])
        except json.JSONDecodeError:
            job["result_payload"] = None
    job["status_label"] = intake_job_status_label(str(job.get("status")))
    job["current_stage_label"] = intake_stage_label(str(job.get("current_stage")))
    return job


def list_recent_intake_jobs(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, object]]:
    items = fetch_all(
        conn,
        """
        SELECT
            id,
            source_kind,
            source_name,
            status,
            current_stage,
            use_council,
            created_at,
            finished_at
        FROM intake_job
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    for item in items:
        item["status_label"] = intake_job_status_label(str(item.get("status")))
        item["current_stage_label"] = intake_stage_label(str(item.get("current_stage")))
    return items


def parse_brief_id(source_ref: str | None) -> int | None:
    if not source_ref or not source_ref.startswith("brief:"):
        return None
    tail = source_ref.split(":", 2)[1]
    try:
        return int(tail)
    except ValueError:
        return None


def extract_quoted_name(details: str | None) -> str | None:
    if not details or details.lstrip().startswith("{"):
        return None
    start = details.find("«")
    end = details.find("»", start + 1) if start >= 0 else -1
    if start < 0 or end < 0:
        return None
    return details[start + 1:end].strip() or None


def repair_intake_review_links(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "review_queue") or not table_exists(conn, "skill_suggestion"):
        return 0

    updated = 0
    rows = conn.execute(
        """
        SELECT id, source_ref, details
        FROM review_queue
        WHERE entity_id IS NULL
          AND source_ref LIKE 'brief:%'
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        brief_id = parse_brief_id(row["source_ref"])
        suggestion_name = extract_quoted_name(row["details"])
        if brief_id is None or not suggestion_name:
            continue
        match_rows = conn.execute(
            """
            SELECT id
            FROM skill_suggestion
            WHERE brief_id = ? AND suggested_name = ?
            ORDER BY id
            """,
            (brief_id, suggestion_name),
        ).fetchall()
        if len(match_rows) != 1:
            continue
        conn.execute("UPDATE review_queue SET entity_id = ? WHERE id = ?", (match_rows[0]["id"], row["id"]))
        updated += 1
    if updated:
        conn.commit()
    return updated


def get_latest_job_id_for_brief(conn: sqlite3.Connection, brief_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM intake_job
        WHERE json_valid(result_payload)
          AND json_extract(result_payload, '$.brief_id') = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (brief_id,),
    ).fetchone()
    return int(row["id"]) if row else None


def get_brief_dag_state(conn: sqlite3.Connection, brief_id: int) -> dict[str, object]:
    accepted_atomic = conn.execute(
        """
        SELECT COUNT(*)
        FROM skill_suggestion
        WHERE brief_id = ?
          AND entity_type = 'skill'
          AND atomicity = 'atomic'
          AND decision = 'accepted'
        """,
        (brief_id,),
    ).fetchone()[0]
    pending_atomic = conn.execute(
        """
        SELECT COUNT(*)
        FROM skill_suggestion
        WHERE brief_id = ?
          AND entity_type = 'skill'
          AND atomicity = 'atomic'
          AND decision = 'needs_review'
        """,
        (brief_id,),
    ).fetchone()[0]
    open_reviews = conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE source_ref = ? AND status = 'open'",
        (f"brief:{brief_id}",),
    ).fetchone()[0]
    prerequisite_rows = conn.execute(
        "SELECT COUNT(*) FROM skill_prerequisite WHERE brief_id = ?",
        (brief_id,),
    ).fetchone()[0] if table_exists(conn, "skill_prerequisite") and column_exists(conn, "skill_prerequisite", "brief_id") else 0
    brief_row = conn.execute(
        "SELECT role, domain FROM profile_brief WHERE id = ?",
        (brief_id,),
    ).fetchone()
    return {
        "brief_id": brief_id,
        "role": brief_row["role"] if brief_row else None,
        "domain": brief_row["domain"] if brief_row else None,
        "latest_job_id": get_latest_job_id_for_brief(conn, brief_id),
        "accepted_atomic_count": int(accepted_atomic),
        "pending_atomic_count": int(pending_atomic),
        "open_review_count": int(open_reviews),
        "prerequisite_count": int(prerequisite_rows),
    }


def build_deferred_dag_payload(state: dict[str, object], *, status: str, message: str) -> dict[str, object]:
    return {
        "status": status,
        "message": message,
        "accepted_atomic_candidates": int(state["accepted_atomic_count"]),
        "pending_atomic_candidates": int(state["pending_atomic_count"]),
        "open_review_count": int(state["open_review_count"]),
        "nodes": 0,
        "edges": 0,
        "removed_cycle": 0,
        "removed_transitive": 0,
        "acyclic": True,
        "waves": [],
        "order": [],
        "final_edges": [],
        "edge_review_queue": [],
        "used_candidate_ids": [],
    }


def update_jobs_dag_payload(
    conn: sqlite3.Connection,
    brief_id: int,
    dag_payload: dict[str, object],
    persisted_update: dict[str, object] | None = None,
) -> None:
    rows = conn.execute(
        """
        SELECT id, result_payload
        FROM intake_job
        WHERE status = 'succeeded'
          AND json_valid(result_payload)
          AND json_extract(result_payload, '$.brief_id') = ?
        """,
        (brief_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["result_payload"])
        payload["dag"] = dag_payload
        if persisted_update and isinstance(payload.get("persisted"), dict):
            payload["persisted"].update(persisted_update)
        conn.execute(
            "UPDATE intake_job SET result_payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), utc_now_iso(), row["id"]),
        )
    conn.commit()


def build_deferred_curriculum_plan_payload(message: str, audience_level: str = "Начальный") -> dict[str, object]:
    return {
        "status": "deferred",
        "message": message,
        "title": "Черновик учебного плана",
        "audience_level": audience_level,
        "source_policy": "accepted_only",
        "summary": {"blocks": 0, "projects": 0, "total_hours": 0, "total_days": 0, "total_xp": 0},
        "rows": [],
        "blocks": [],
        "csv_primary_header": [],
        "csv_secondary_header": [],
        "report": {"coverage_ok": False, "order_violations": []},
    }


def update_jobs_curriculum_plan_payload(
    conn: sqlite3.Connection,
    brief_id: int,
    plan_payload: dict[str, object],
    persisted_update: dict[str, object] | None = None,
) -> None:
    rows = conn.execute(
        """
        SELECT id, result_payload
        FROM intake_job
        WHERE status = 'succeeded'
          AND json_valid(result_payload)
          AND json_extract(result_payload, '$.brief_id') = ?
        """,
        (brief_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["result_payload"])
        payload["curriculum_plan"] = plan_payload
        if persisted_update and isinstance(payload.get("persisted"), dict):
            payload["persisted"].update(persisted_update)
        conn.execute(
            "UPDATE intake_job SET result_payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), utc_now_iso(), row["id"]),
        )
    conn.commit()


def clear_brief_dag_artifacts(conn: sqlite3.Connection, brief_id: int) -> None:
    if table_exists(conn, "skill_prerequisite") and column_exists(conn, "skill_prerequisite", "brief_id"):
        conn.execute("DELETE FROM skill_prerequisite WHERE brief_id = ?", (brief_id,))
    if table_exists(conn, "review_queue"):
        conn.execute(
            """
            DELETE FROM review_queue
            WHERE source_ref = ?
              AND json_valid(details)
              AND json_extract(details, '$.review_kind') = 'prerequisite_edge'
            """,
            (f"brief:{brief_id}",),
        )
    conn.commit()


def clear_brief_curriculum_plan_artifacts(conn: sqlite3.Connection, brief_id: int) -> None:
    if table_exists(conn, "curriculum_plan_row"):
        conn.execute(
            """
            DELETE FROM curriculum_plan_row
            WHERE plan_id IN (SELECT id FROM curriculum_plan WHERE brief_id = ?)
            """,
            (brief_id,),
        )
    if table_exists(conn, "curriculum_plan"):
        conn.execute("DELETE FROM curriculum_plan WHERE brief_id = ?", (brief_id,))
    conn.commit()


def refresh_brief_dag_state(
    conn: sqlite3.Connection,
    brief_id: int,
    *,
    status: str = "deferred",
    message: str | None = None,
) -> dict[str, object]:
    state = get_brief_dag_state(conn, brief_id)
    if message is None:
        if state["accepted_atomic_count"]:
            message = "Граф будет пересчитан по текущему набору принятых атомарных навыков."
            status = "stale" if state["prerequisite_count"] else status
        else:
            message = "Граф пока пуст: нет принятых атомарных навыков."
    dag_payload = build_deferred_dag_payload(state, status=status, message=message)
    update_jobs_dag_payload(
        conn,
        brief_id,
        dag_payload,
        persisted_update={
            "skill_prerequisite": 0,
            "prerequisite_reviews": 0,
            "review_open": int(state["open_review_count"]),
        },
    )
    return state


def hydrate_job_result_payload(conn: sqlite3.Connection, result: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return result
    brief_id = result.get("brief_id")
    if not isinstance(brief_id, int) or not isinstance(result.get("candidates"), list):
        return result
    from spravochnik_intake.pipeline import config as intake_config

    suggestion_rows = conn.execute(
        """
        SELECT id, suggested_name, group_name, entity_type, atomicity, decision, confidence, council_agreement, resolution
        FROM skill_suggestion
        WHERE brief_id = ?
        ORDER BY id
        """,
        (brief_id,),
    ).fetchall()
    rows_by_key: dict[tuple[str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    id_to_row: dict[int, sqlite3.Row] = {}
    for row in suggestion_rows:
        key = (
            str(row["suggested_name"] or ""),
            str(row["group_name"] or ""),
            str(row["entity_type"] or ""),
            str(row["atomicity"] or ""),
        )
        rows_by_key[key].append(row)
        id_to_row[int(row["id"])] = row

    review_status_by_entity: dict[int, str] = {}
    for row in conn.execute(
        """
        SELECT entity_id, status
        FROM review_queue
        WHERE source_ref = ?
          AND entity_id IS NOT NULL
        ORDER BY id
        """,
        (f"brief:{brief_id}",),
    ):
        review_status_by_entity[int(row["entity_id"])] = str(row["status"])

    coverage_by_name: dict[str, str] = {}
    if isinstance(result.get("coverage"), dict):
        for row in result["coverage"].get("rows", []):
            if not isinstance(row, dict):
                continue
            area = str(row.get("area") or "").strip()
            if not area:
                continue
            for candidate_name in row.get("candidate_names") or []:
                name = str(candidate_name or "").strip()
                if name:
                    coverage_by_name[name] = area

    for candidate in result["candidates"]:
        if not isinstance(candidate, dict):
            continue
        suggestion_id = candidate.get("suggestion_id")
        row = id_to_row.get(int(suggestion_id)) if isinstance(suggestion_id, int) else None
        if row is None:
            key = (
                str(candidate.get("name") or ""),
                str(candidate.get("group") or ""),
                str(candidate.get("entity_type") or ""),
                str(candidate.get("atomicity") or ""),
            )
            row_list = rows_by_key.get(key)
            row = row_list.pop(0) if row_list else None
        if row is None:
            continue
        suggestion_id = int(row["id"])
        candidate["suggestion_id"] = suggestion_id
        candidate["decision"] = str(row["decision"] or candidate.get("decision") or "pending")
        confidence_value = float(row["confidence"]) if row["confidence"] is not None else None
        council_agreement_value = float(row["council_agreement"]) if row["council_agreement"] is not None else None
        candidate["confidence"] = f"{confidence_value:.2f}" if confidence_value is not None else "—"
        candidate["council_agreement"] = f"{council_agreement_value:.2f}" if council_agreement_value is not None else None
        candidate["resolution"] = row["resolution"] or candidate.get("resolution")
        default_review_status = (
            "resolved"
            if candidate["decision"] == "accepted"
            else ("ignored" if candidate["decision"] == "rejected" else "open")
        )
        candidate["review_status"] = review_status_by_entity.get(suggestion_id, default_review_status)
        candidate["can_review_inline"] = candidate.get("entity_type") == "skill" and candidate.get("atomicity") == "atomic"
        if not candidate.get("coverage_area"):
            parent_name = str(candidate.get("parent_name") or "").strip()
            own_name = str(candidate.get("name") or "").strip()
            candidate["coverage_area"] = coverage_by_name.get(parent_name) or coverage_by_name.get(own_name)
        if (
            candidate["decision"] == "accepted"
            and confidence_value is not None
            and confidence_value >= intake_config.AUTO_ACCEPT_CONFIDENCE
            and council_agreement_value is not None
            and council_agreement_value >= intake_config.AUTO_ACCEPT_COUNCIL_AGREEMENT
        ):
            candidate["reasons"] = review_reason_label("auto_accept_policy")

    if isinstance(result.get("council_metrics"), dict):
        candidates = [item for item in result["candidates"] if isinstance(item, dict)]
        resolved_candidates = [
            item
            for item in candidates
            if item.get("entity_type") == "skill" and item.get("atomicity") == "atomic"
        ]
        council_candidates = [item for item in resolved_candidates if item.get("council_agreement") not in {None, "", "—"}]
        result["council_metrics"].update(
            {
                "sent_to_council": len(council_candidates),
                "auto_accepted": len(
                    [item for item in resolved_candidates if item.get("decision") == "accepted" and item.get("council_agreement") in {None, "", "—"}]
                ),
                "accepted_after_council": len(
                    [item for item in council_candidates if item.get("decision") == "accepted"]
                ),
                "review_after_council": len(
                    [item for item in council_candidates if item.get("decision") == "needs_review"]
                ),
                "needs_review_total": len([item for item in candidates if item.get("decision") == "needs_review"]),
                "accepted_total": len([item for item in resolved_candidates if item.get("decision") == "accepted"]),
                "matched_total": len([item for item in resolved_candidates if item.get("resolution") == "matched"]),
                "alias_total": len([item for item in resolved_candidates if item.get("resolution") == "alias"]),
                "fuzzy_total": len([item for item in resolved_candidates if item.get("resolution") == "fuzzy"]),
                "new_total": len([item for item in resolved_candidates if item.get("resolution") == "new"]),
            }
        )

    if not isinstance(result.get("curriculum_plan"), dict):
        dag_payload = result.get("dag") if isinstance(result.get("dag"), dict) else None
        result["curriculum_plan"] = build_curriculum_plan_for_brief(conn, brief_id, dag_payload=dag_payload)

    if isinstance(result.get("persisted"), dict):
        result["persisted"]["review_open"] = int(get_brief_dag_state(conn, brief_id)["open_review_count"])
        result["persisted"]["curriculum_plan_rows"] = int(result.get("curriculum_plan", {}).get("row_count", 0) or 0)
    return result


def apply_candidate_decision(
    conn: sqlite3.Connection,
    suggestion_id: int,
    target_decision: str,
    resolution_note: str | None = None,
) -> int | None:
    row = conn.execute(
        """
        SELECT id, brief_id
        FROM skill_suggestion
        WHERE id = ?
        """,
        (suggestion_id,),
    ).fetchone()
    if not row:
        return None

    brief_id = int(row["brief_id"])
    review_status_map = {
        "accepted": "resolved",
        "needs_review": "open",
        "rejected": "ignored",
    }
    review_status = review_status_map.get(target_decision, "open")
    now = utc_now_iso()
    reviewed_at = None if review_status == "open" else now
    conn.execute(
        "UPDATE skill_suggestion SET decision = ? WHERE id = ?",
        (target_decision, suggestion_id),
    )
    conn.execute(
        """
        UPDATE review_queue
        SET status = ?,
            resolution_note = COALESCE(?, resolution_note),
            reviewed_at = ?,
            updated_at = ?
        WHERE source_ref = ?
          AND entity_id = ?
        """,
        (review_status, resolution_note, reviewed_at, now, f"brief:{brief_id}", suggestion_id),
    )
    clear_brief_dag_artifacts(conn, brief_id)
    conn.commit()
    build_dag_for_brief(conn, brief_id)
    return brief_id


def load_accepted_skill_candidates(conn: sqlite3.Connection, brief_id: int):
    from spravochnik_intake.pipeline.models import IndicatorSpec, SkillCandidate

    rows = conn.execute(
        """
        SELECT
            ss.id,
            ss.suggested_name,
            ss.group_name,
            ss.coverage_area,
            ss.bloom,
            ss.indicators_json,
            ss.tools,
            ss.evidence_ids,
            ss.resolution,
            ss.canonical_skill_id,
            ss.confidence,
            ss.council_agreement
        FROM skill_suggestion ss
        WHERE ss.brief_id = ?
          AND ss.entity_type = 'skill'
          AND ss.atomicity = 'atomic'
          AND ss.decision = 'accepted'
        ORDER BY ss.id
        """,
        (brief_id,),
    ).fetchall()

    bloom_fallback = {"remember", "understand", "apply", "analyze", "evaluate", "create"}
    cands = []
    tmp_to_db: dict[str, int] = {}
    for row in rows:
        bloom_label = str(row["bloom"] or "remember").strip().casefold()
        if bloom_label not in bloom_fallback:
            bloom_label = "remember"
        raw_indicators = json.loads(row["indicators_json"] or "[]")
        indicators = []
        for item in raw_indicators:
            if not isinstance(item, dict):
                continue
            indicator_bloom = str(item.get("bloom") or bloom_label).strip().casefold()
            if indicator_bloom not in bloom_fallback:
                indicator_bloom = bloom_label
            indicators.append(
                IndicatorSpec(
                    text=str(item.get("text") or row["suggested_name"]),
                    bloom=indicator_bloom,
                )
            )
        if not indicators:
            indicators = [IndicatorSpec(text=row["suggested_name"], bloom=bloom_label)]
        tmp_id = f"S{row['id']}"
        candidate = SkillCandidate(
            tmp_id=tmp_id,
            name=row["suggested_name"],
            group=row["group_name"] or "Без группы",
            coverage_area=row["coverage_area"],
            indicators=indicators,
            tools=json.loads(row["tools"] or "[]"),
            evidence_ids=[str(item) for item in json.loads(row["evidence_ids"] or "[]") if item is not None],
            confidence=float(row["confidence"] or 0.0),
            council_agreement=float(row["council_agreement"]) if row["council_agreement"] is not None else None,
            entity_type="skill",
            atomicity="atomic",
            resolution=row["resolution"],
            canonical_skill_id=row["canonical_skill_id"],
            decision="accepted",
        )
        cands.append(candidate)
        tmp_to_db[tmp_id] = int(row["id"])
    return cands, tmp_to_db


def load_brief_spec_for_plan(conn: sqlite3.Connection, brief_id: int) -> dict[str, object]:
    row = conn.execute(
        "SELECT role, seniority, domain FROM profile_brief WHERE id = ?",
        (brief_id,),
    ).fetchone()
    if not row:
        return {}
    return {
        "role": row["role"],
        "seniority": row["seniority"],
        "domain": row["domain"],
    }


def build_curriculum_plan_for_brief(
    conn: sqlite3.Connection,
    brief_id: int,
    candidates: list[object] | None = None,
    dag_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    from spravochnik_intake.pipeline import stage_dag_to_up, storage

    clear_brief_curriculum_plan_artifacts(conn, brief_id)
    accepted_candidates, _tmp_to_db = load_accepted_skill_candidates(conn, brief_id)
    cands = accepted_candidates if candidates is None else candidates
    effective_dag_payload = dag_payload or build_deferred_dag_payload(get_brief_dag_state(conn, brief_id), status="deferred", message="DAG не построен")
    spec = load_brief_spec_for_plan(conn, brief_id)
    plan_payload = stage_dag_to_up.run(spec, cands, effective_dag_payload)
    save_meta = storage.save_curriculum_plan(conn, brief_id, plan_payload)
    plan_payload["plan_id"] = save_meta["plan_id"]
    plan_payload["row_count"] = save_meta["row_count"]
    update_jobs_curriculum_plan_payload(
        conn,
        brief_id,
        plan_payload,
        persisted_update={"curriculum_plan_rows": save_meta["row_count"]},
    )
    return plan_payload


def build_dag_for_brief(conn: sqlite3.Connection, brief_id: int) -> dict[str, object]:
    from spravochnik_intake.pipeline import stage_catalog_to_dag, storage

    clear_brief_dag_artifacts(conn, brief_id)
    cands, tmp_to_db = load_accepted_skill_candidates(conn, brief_id)
    if not cands:
        clear_brief_curriculum_plan_artifacts(conn, brief_id)
        plan_payload = build_deferred_curriculum_plan_payload(
            "Черновик УП пока не строится: ещё нет принятых навыков с валидным DAG."
        )
        save_meta = storage.save_curriculum_plan(conn, brief_id, plan_payload)
        plan_payload["plan_id"] = save_meta["plan_id"]
        plan_payload["row_count"] = save_meta["row_count"]
        state = refresh_brief_dag_state(
            conn,
            brief_id,
            status="deferred",
            message="Граф пока пуст: ещё нет принятых атомарных навыков. Он построится автоматически после первого принятия.",
        )
        update_jobs_curriculum_plan_payload(
            conn,
            brief_id,
            plan_payload,
            persisted_update={"curriculum_plan_rows": 0},
        )
        return {
            "brief_id": brief_id,
            "state": state,
            "dag": build_deferred_dag_payload(
                state,
                status="deferred",
                message="Граф пока пуст: ещё нет принятых атомарных навыков. Он построится автоматически после первого принятия.",
            ),
            "curriculum_plan": plan_payload,
        }

    edges, dag, removed_cycle, removed_transitive, dag_payload = stage_catalog_to_dag.run(cands)
    prereq_count = storage.save_prerequisites(conn, brief_id, dag, cands, tmp_to_db)
    prereq_review_count = storage.save_prerequisite_reviews(conn, brief_id, dag_payload["edge_review_queue"])
    dag_payload["status"] = "built"
    dag_payload["message"] = "Граф построен по текущему набору принятых атомарных навыков и пересчитывается автоматически."
    dag_payload["accepted_atomic_candidates"] = len(cands)
    dag_payload["prerequisite_rows"] = prereq_count
    dag_payload["prerequisite_review_rows"] = prereq_review_count
    plan_payload = build_curriculum_plan_for_brief(conn, brief_id, cands, dag_payload)
    update_jobs_dag_payload(
        conn,
        brief_id,
        dag_payload,
        persisted_update={
            "skill_prerequisite": prereq_count,
            "prerequisite_reviews": prereq_review_count,
            "review_open": int(get_brief_dag_state(conn, brief_id)["open_review_count"]),
        },
    )
    return {
        "brief_id": brief_id,
        "state": get_brief_dag_state(conn, brief_id),
        "dag": dag_payload,
        "curriculum_plan": plan_payload,
        "edges": len(edges),
        "removed_cycle": len(removed_cycle),
        "removed_transitive": len(removed_transitive),
    }


def list_dag_build_options(conn: sqlite3.Connection) -> list[dict[str, object]]:
    if not table_exists(conn, "profile_brief") or not table_exists(conn, "skill_suggestion"):
        return []
    rows = conn.execute(
        """
        SELECT pb.id, pb.role, pb.domain
        FROM profile_brief pb
        WHERE EXISTS (SELECT 1 FROM skill_suggestion ss WHERE ss.brief_id = pb.id)
        ORDER BY pb.id DESC
        """
    ).fetchall()
    options = []
    for row in rows:
        state = get_brief_dag_state(conn, int(row["id"]))
        options.append(state)
    return options


def ensure_target_runtime_schema(conn: sqlite3.Connection) -> None:
    skill_columns = {
        "sort_order": "INTEGER NOT NULL DEFAULT 999",
        "complexity_min_band": "TEXT",
        "complexity_max_band": "TEXT",
        "complexity_summary": "TEXT",
        "source_scale_title": "TEXT",
    }
    indicator_columns = {
        "complexity_band": "TEXT",
        "complexity_label": "TEXT",
        "complexity_sort_order": "INTEGER",
        "source_scale_title": "TEXT",
    }

    if table_exists(conn, "skill") and not column_exists(conn, "skill", "sort_order"):
        conn.execute("ALTER TABLE skill ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 999")

        skill_rows = conn.execute(
            """
            SELECT id, group_id
            FROM skill
            ORDER BY group_id, id
            """
        ).fetchall()
        current_group_id = None
        next_sort_order = 0
        for row in skill_rows:
            if row["group_id"] != current_group_id:
                current_group_id = row["group_id"]
                next_sort_order = 1
            else:
                next_sort_order += 1
            conn.execute("UPDATE skill SET sort_order = ? WHERE id = ?", (next_sort_order, row["id"]))

    if table_exists(conn, "skill"):
        for column_name, column_type in skill_columns.items():
            if not column_exists(conn, "skill", column_name):
                conn.execute(f"ALTER TABLE skill ADD COLUMN {column_name} {column_type}")

    if table_exists(conn, "indicator"):
        for column_name, column_type in indicator_columns.items():
            if not column_exists(conn, "indicator", column_name):
                conn.execute(f"ALTER TABLE indicator ADD COLUMN {column_name} {column_type}")

    if table_exists(conn, "skill"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_skill_group_id
            ON skill (group_id, is_active, sort_order, name)
            """
        )

    if table_exists(conn, "skill") and table_exists(conn, "indicator"):
        for row in conn.execute("SELECT id FROM skill ORDER BY id"):
            refresh_target_skill_complexity(conn, row["id"], commit=False)
    conn.commit()


def decode_uploaded_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_docx_text(data: bytes) -> str:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document_xml = archive.read("word/document.xml")
    root = ET.fromstring(document_xml)
    for paragraph in root.findall(".//w:p", namespace):
        texts = [node.text for node in paragraph.findall(".//w:t", namespace) if node.text]
        line = "".join(texts).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def extract_csv_text(data: bytes) -> str:
    decoded = decode_uploaded_text(data)
    sample = decoded[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    rows: list[str] = []
    reader = csv.reader(io.StringIO(decoded), dialect)
    for row in reader:
        cells = [cell.replace("\ufeff", "").strip() for cell in row]
        non_empty = [cell for cell in cells if cell]
        if not non_empty:
            continue
        if len(non_empty) == 1:
            rows.append(non_empty[0])
            continue
        head, tail = non_empty[0], non_empty[1:]
        if len(tail) == 1:
            rows.append(f"{head}: {tail[0]}")
            continue
        rows.append(f"{head}: {' | '.join(tail)}")
    return "\n\n".join(rows)


def extract_brief_text_from_bytes(data: bytes, suffix: str) -> str:
    if suffix in {".txt", ".md"}:
        return decode_uploaded_text(data).strip()
    if suffix == ".csv":
        return extract_csv_text(data).strip()
    if suffix == ".docx":
        return extract_docx_text(data).strip()
    raise ValueError("Поддерживаются только файлы .txt, .md, .csv и .docx.")


def load_brief_text_from_path(file_path_raw: str) -> tuple[str, str]:
    file_path = Path(file_path_raw.strip().strip('"')).expanduser()
    if not file_path.exists():
        raise ValueError(f"Файл не найден: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"Указанный путь не является файлом: {file_path}")

    suffix = file_path.suffix.casefold()
    data = file_path.read_bytes()
    return extract_brief_text_from_bytes(data, suffix), file_path.name


def normalize_existing_brief_file_path(file_path_raw: str | None) -> str:
    if not file_path_raw:
        return ""
    file_path = Path(file_path_raw.strip().strip('"')).expanduser()
    if not file_path.exists() or not file_path.is_file():
        return ""
    return str(file_path)


def load_brief_text(
    form_data: dict[str, str],
    files: dict[str, UploadedFile],
) -> tuple[str, str | None, str, str | None]:
    uploaded_file = files.get("brief_file")
    if uploaded_file:
        suffix = Path(uploaded_file.filename).suffix.casefold()
        brief_text = extract_brief_text_from_bytes(uploaded_file.data, suffix)
        return brief_text, uploaded_file.filename, "file", None

    file_path_raw = form_data.get("brief_file_path", "").strip()
    if file_path_raw:
        try:
            brief_text, source_name = load_brief_text_from_path(file_path_raw)
            return brief_text, source_name, "file", file_path_raw
        except ValueError:
            brief_text = form_data.get("brief", "").strip()
            if brief_text:
                return brief_text, None, "text", None
            raise

    brief_text = form_data.get("brief", "").strip()
    if brief_text:
        return brief_text, None, "text", None

    return "", None, "text", None


def run_intake_pipeline(
    conn: sqlite3.Connection,
    db_path: Path,
    brief_text: str,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, object]:
    from spravochnik_intake.pipeline import stage_brief_to_catalog, stage_normalize, storage
    from spravochnik_intake.pipeline import config as intake_config
    from spravochnik_intake.pipeline.catalog_repo import CatalogRepo

    ensure_intake_runtime_schema(conn, db_path)
    storage.apply_migration(conn, str(INTAKE_SCHEMA_SQL))

    def notify(stage: str, note: str) -> None:
        if progress_callback:
            progress_callback(stage, note)

    repo = CatalogRepo(str(db_path))
    try:
        notify("decompose", "Декомпозиция свободного брифа в роль, уровень и поисковые подзапросы.")
        spec = stage_brief_to_catalog.decompose(brief_text)
        notify("search", "Сбор внешних evidence по подзапросам.")
        evidence = stage_brief_to_catalog.gather_evidence(spec["sub_queries"])
        notify("synthesize", "Синтез навыков-кандидатов и индикаторов по найденным evidence.")
        raw_candidates, coverage = stage_brief_to_catalog.synthesize_with_coverage(evidence, spec)
        notify("atomize", "Проверка атомарности кандидатов, разбиение составных формулировок и реклассификация не-навыков.")
        atomized_candidates = stage_brief_to_catalog.atomize_candidates(raw_candidates, spec)
        notify("normalize", "Нормализация названий и безопасное схлопывание дублирующих atomic skills.")
        candidates, normalize_report = stage_normalize.run(atomized_candidates, spec)
        notify("resolve", "Сопоставление навыков-кандидатов с текущим каталогом.")
        stage_brief_to_catalog.resolve_candidates(candidates, evidence, repo)
        council_metrics_preview = {
            "sent_to_council": len(stage_brief_to_catalog.select_council_candidates(candidates)),
        }
        if intake_config.USE_COUNCIL and council_metrics_preview["sent_to_council"] > 0:
            notify(
                "council",
                f"Экспертное жюри проверяет спорные навыки: {council_metrics_preview['sent_to_council']} кандидатов.",
            )
            stage_brief_to_catalog.run_council(candidates)
        else:
            notify("council", "Council не потребовался: спорных навыков для panel нет.")
        notify("triage", "Финальный триаж: что принять автоматически, а что отправить на review.")
        stage_brief_to_catalog.triage_candidates(candidates, spec)
        candidate_metrics = stage_brief_to_catalog.build_candidate_metrics(candidates)
    finally:
        repo.con.close()

    notify("persist", "Запись результатов в каталог и очередь проверки.")
    brief_id = storage.save_brief(conn, brief_text, spec)
    evidence_map = storage.save_evidence(conn, brief_id, evidence)
    tmp_to_db = storage.save_suggestions(conn, brief_id, candidates, evidence_map)
    by_tid = {candidate.tmp_id: candidate for candidate in candidates}
    atomize_events = []
    for candidate in atomized_candidates:
        if candidate.atomicity == "composite":
            atomize_events.append(
                {
                    "parent_name": candidate.name,
                    "verdict": "composite",
                    "children": [child.name for child in atomized_candidates if child.parent_tmp_id == candidate.tmp_id],
                    "rationale": candidate.atomize_rationale,
                }
            )
        elif candidate.atomicity == "non_skill":
            atomize_events.append(
                {
                    "parent_name": candidate.name,
                    "verdict": "non_skill",
                    "entity_type": candidate.entity_type,
                    "children": [],
                    "rationale": candidate.atomize_rationale,
                }
            )

    notify("plan", "Сборка DAG и черновика учебного плана по принятым навыкам.")
    dag_build_result = build_dag_for_brief(conn, brief_id)
    dag_payload = dag_build_result["dag"]
    dag_state = dag_build_result["state"]
    curriculum_plan = dag_build_result.get("curriculum_plan", build_deferred_curriculum_plan_payload("Черновик УП пока не сформирован."))

    return {
        "brief_id": brief_id,
        "spec": spec,
        "candidates": [
            {
                "name": candidate.name,
                "group": candidate.group,
                "coverage_area": candidate.coverage_area or (
                    by_tid[candidate.parent_tmp_id].coverage_area
                    if candidate.parent_tmp_id and candidate.parent_tmp_id in by_tid
                    else None
                ),
                "bloom": candidate.bloom,
                "entity_type": candidate.entity_type,
                "atomicity": candidate.atomicity,
                "suggestion_id": tmp_to_db.get(candidate.tmp_id),
                "parent_tmp_id": candidate.parent_tmp_id,
                "parent_name": by_tid[candidate.parent_tmp_id].name if candidate.parent_tmp_id and candidate.parent_tmp_id in by_tid else None,
                "resolution": candidate.resolution,
                "canonical_name": candidate.canonical_name,
                "confidence": f"{candidate.confidence:.2f}" if candidate.confidence else "—",
                "council_agreement": None if candidate.council_agreement is None else f"{candidate.council_agreement:.2f}",
                "decision": candidate.decision,
                "review_status": "open" if candidate.decision == "needs_review" else ("resolved" if candidate.decision == "accepted" else "ignored"),
                "can_review_inline": candidate.entity_type == "skill" and candidate.atomicity == "atomic",
                "reasons": ", ".join(review_reason_label(reason) for reason in candidate.reasons) if candidate.reasons else "",
                "tools": ", ".join(candidate.tools) if candidate.tools else "—",
            }
            for candidate in candidates
            if candidate.atomicity in {"atomic", "non_skill"}
        ],
        "atomize": {
            "raw_count": len(raw_candidates),
            "atomic_count": len([candidate for candidate in atomized_candidates if candidate.atomicity == "atomic"]),
            "composite_count": len([candidate for candidate in atomized_candidates if candidate.atomicity == "composite"]),
            "non_skill_count": len([candidate for candidate in atomized_candidates if candidate.atomicity == "non_skill"]),
            "events": atomize_events,
        },
        "normalize": normalize_report,
        "coverage": coverage,
        "dag": dag_payload,
        "curriculum_plan": curriculum_plan,
        "persisted": {
            "evidence_source": len(evidence),
            "skill_suggestion": len(candidates),
            "skill_prerequisite": int(dag_payload.get("prerequisite_rows", 0) or 0),
            "prerequisite_reviews": int(dag_payload.get("prerequisite_review_rows", 0) or 0),
            "curriculum_plan_rows": int(curriculum_plan.get("row_count", 0) or 0),
            "review_open": int(dag_state["open_review_count"]),
        },
        "meta": {
            "use_live": intake_config.USE_LIVE,
            "use_council": intake_config.USE_COUNCIL,
            "model_plan": intake_config.MODEL_PLAN,
            "model_search": intake_config.MODEL_SEARCH,
            "model_panel": intake_config.MODEL_PANEL,
        },
        "council_metrics": candidate_metrics,
    }


def execute_intake_job(db_path: Path, job_id: int) -> None:
    ACTIVE_INTAKE_JOB_IDS.add(job_id)
    conn = open_db(db_path)
    try:
        ensure_intake_runtime_schema(conn, db_path)
        job = get_intake_job(conn, job_id)
        if not job:
            return

        update_intake_job(
            conn,
            job_id,
            status="running",
            current_stage="starting",
            progress_note="Запуск intake-пайплайна.",
            mark_started=True,
        )

        def progress(stage: str, note: str) -> None:
            worker_conn = open_db(db_path)
            try:
                ensure_intake_runtime_schema(worker_conn, db_path)
                update_intake_job(worker_conn, job_id, current_stage=stage, progress_note=note)
            finally:
                worker_conn.close()

        result = run_intake_pipeline(conn, db_path, str(job["brief_text"]), progress_callback=progress)
        update_intake_job(
            conn,
            job_id,
            status="succeeded",
            current_stage="completed",
            progress_note="Обработка завершена.",
            result_payload=result,
            mark_finished=True,
        )
    except Exception as exc:
        update_intake_job(
            conn,
            job_id,
            status="failed",
            current_stage="failed",
            progress_note="Пайплайн завершился с ошибкой.",
            error_text=str(exc),
            mark_finished=True,
        )
    finally:
        conn.close()
        ACTIVE_INTAKE_JOB_IDS.discard(job_id)


def queue_intake_job(db_path: Path, job_id: int) -> None:
    ACTIVE_INTAKE_JOB_IDS.add(job_id)
    INTAKE_EXECUTOR.submit(execute_intake_job, db_path, job_id)


def open_native_brief_picker() -> dict[str, object]:
    """Открывает системный диалог выбора файла на Windows и возвращает путь."""
    initial_dir = str((Path.home() / "Desktop").resolve()) if (Path.home() / "Desktop").exists() else str(PROJECT_ROOT)
    script = rf"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Выберите бриф'
$dialog.Filter = 'Документы брифа|*.txt;*.md;*.csv;*.docx|Все файлы|*.*'
$dialog.Multiselect = $false
$dialog.InitialDirectory = '{initial_dir.replace("'", "''")}'
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $dialog.FileName
}}
"""
    try:
        completed = subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Sta", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Не удалось открыть системный диалог: {exc}"}

    if completed.returncode != 0:
        error_text = (completed.stderr or completed.stdout or "").strip() or "PowerShell завершился с ошибкой."
        return {"ok": False, "error": error_text}

    selected_path = (completed.stdout or "").strip()
    if not selected_path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": selected_path, "name": Path(selected_path).name}


def complexity_label_for_band(band: str | None) -> str | None:
    if not band:
        return None
    return COMPLEXITY_LABELS.get(band, band.replace("_", " "))


def build_complexity_summary(
    min_band: str | None,
    max_band: str | None,
    min_label: str | None,
    max_label: str | None,
) -> str | None:
    if not min_band and not max_band:
        return None
    left = min_label or complexity_label_for_band(min_band)
    right = max_label or complexity_label_for_band(max_band)
    if not left:
        return right
    if not right or left == right:
        return left
    return f"{left} -> {right}"


def refresh_target_skill_complexity(conn: sqlite3.Connection, skill_id: int, commit: bool = True) -> None:
    rows = conn.execute(
        """
        SELECT
            complexity_band,
            complexity_label,
            complexity_sort_order,
            source_scale_title
        FROM indicator
        WHERE skill_id = ?
          AND complexity_sort_order IS NOT NULL
        ORDER BY complexity_sort_order, id
        """,
        (skill_id,),
    ).fetchall()

    if not rows:
        conn.execute(
            """
            UPDATE skill
            SET complexity_min_band = NULL,
                complexity_max_band = NULL,
                complexity_summary = NULL,
                source_scale_title = NULL
            WHERE id = ?
            """,
            (skill_id,),
        )
        if commit:
            conn.commit()
        return

    min_row = rows[0]
    max_row = rows[-1]
    scale_titles = {row["source_scale_title"] for row in rows if row["source_scale_title"]}
    scale_title = next(iter(scale_titles)) if len(scale_titles) == 1 else ("Смешанная шкала" if scale_titles else None)
    complexity_summary = build_complexity_summary(
        min_row["complexity_band"],
        max_row["complexity_band"],
        min_row["complexity_label"],
        max_row["complexity_label"],
    )
    conn.execute(
        """
        UPDATE skill
        SET complexity_min_band = ?,
            complexity_max_band = ?,
            complexity_summary = ?,
            source_scale_title = ?
        WHERE id = ?
        """,
        (
            min_row["complexity_band"],
            max_row["complexity_band"],
            complexity_summary,
            scale_title,
            skill_id,
        ),
    )
    if commit:
        conn.commit()


def update_review_status(conn: sqlite3.Connection, review_id: int, new_status: str, resolution_note: str) -> None:
    repair_intake_review_links(conn)
    review_row = conn.execute(
        """
        SELECT id, entity_id, source_ref, reason_code
        FROM review_queue
        WHERE id = ?
        """,
        (review_id,),
    ).fetchone()
    if not review_row:
        return

    reviewed_at = datetime.now(UTC).isoformat() if new_status != "open" else None
    conn.execute(
        """
        UPDATE review_queue
        SET status = ?,
            resolution_note = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (new_status, resolution_note.strip() or None, reviewed_at, datetime.now(UTC).isoformat(), review_id),
    )
    brief_id = parse_brief_id(review_row["source_ref"])
    suggestion_id = review_row["entity_id"]
    if suggestion_id and brief_id is not None:
        mapped_decision = "needs_review"
        if new_status == "resolved":
            mapped_decision = "accepted"
        elif new_status == "ignored":
            mapped_decision = "rejected"
        conn.execute(
            "UPDATE skill_suggestion SET decision = ? WHERE id = ?",
            (mapped_decision, suggestion_id),
        )
        clear_brief_dag_artifacts(conn, brief_id)
    conn.commit()
    if brief_id is not None:
        build_dag_for_brief(conn, brief_id)


def slugify(value: str) -> str:
    lowered = value.casefold().replace("ё", "е")
    lowered = "-".join(part for part in "".join(ch if ch.isalnum() else "-" for ch in lowered).split("-") if part)
    return lowered or "item"


def curriculum_plan_status_label(status: str | None) -> str:
    mapping = {
        "draft": "Черновик",
        "built": "Собран",
        "deferred": "Отложен",
    }
    return mapping.get((status or "").strip().casefold(), "Неизвестно")


def curriculum_plan_to_csv_bytes(plan_payload: dict[str, object]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    primary_header = plan_payload.get("csv_primary_header") or []
    secondary_header = plan_payload.get("csv_secondary_header") or []
    if isinstance(primary_header, list) and primary_header:
        writer.writerow(primary_header)
    if isinstance(secondary_header, list) and secondary_header:
        writer.writerow(secondary_header)
    for row in plan_payload.get("rows", []):
        if not isinstance(row, dict):
            continue
        writer.writerow(
            [
                row.get("block_title", ""),
                row.get("block_goal", ""),
                row.get("row_number", ""),
                row.get("project_name", ""),
                row.get("project_summary", ""),
                row.get("learning_outcomes", ""),
                row.get("skills_list", ""),
                row.get("audience_level", ""),
                row.get("required_tools", ""),
                row.get("storytelling", ""),
                row.get("delivery_format", ""),
                row.get("group_size", ""),
                row.get("effort_hours", ""),
                row.get("effort_days", ""),
                row.get("cumulative_days", ""),
                row.get("xp", ""),
                row.get("platform_project_name", ""),
                row.get("artifact_links", ""),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def load_curriculum_plan_rows(conn: sqlite3.Connection, plan_id: int) -> list[dict[str, object]]:
    if not table_exists(conn, "curriculum_plan_row"):
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM curriculum_plan_row
        WHERE plan_id = ?
        ORDER BY row_number ASC, id ASC
        """,
        (plan_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_curriculum_plan_payload_from_rows(
    plan_meta: dict[str, object],
    rows: list[dict[str, object]],
) -> dict[str, object]:
    from spravochnik_intake.pipeline.stage_dag_to_up import CSV_PRIMARY_HEADER, CSV_SECONDARY_HEADER

    payload = {}
    if isinstance(plan_meta.get("payload_json"), str) and plan_meta.get("payload_json"):
        try:
            payload = json.loads(str(plan_meta["payload_json"]))
        except json.JSONDecodeError:
            payload = {}

    total_hours = sum(float(row.get("effort_hours", 0) or 0) for row in rows)
    total_days = sum(float(row.get("effort_days", 0) or 0) for row in rows)
    total_xp = sum(int(row.get("xp", 0) or 0) for row in rows)

    rows_by_block: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        rows_by_block.setdefault(int(row.get("block_index", 0) or 0), []).append(row)

    block_payloads: list[dict[str, object]] = []
    for block_index in sorted(rows_by_block):
        block_rows = sorted(rows_by_block[block_index], key=lambda item: (int(item.get("row_number", 0) or 0), int(item.get("id", 0) or 0)))
        block_payloads.append(
            {
                "block_index": block_index,
                "title": str(block_rows[0].get("block_title") or f"Блок {block_index or 1}"),
                "goal": str(block_rows[0].get("block_goal") or ""),
                "project_count": len(block_rows),
                "total_hours": sum(float(item.get("effort_hours", 0) or 0) for item in block_rows),
                "total_days": round(sum(float(item.get("effort_days", 0) or 0) for item in block_rows), 2),
                "rows": block_rows,
            }
        )

    status = str(plan_meta.get("status") or "draft")
    if rows and status == "deferred":
        status = "draft"
    default_message = "Черновик УП доступен для ручной доработки." if rows else "Черновик УП пока не построен."
    message = str(payload.get("message") or default_message)
    if rows and "пока не стро" in message.casefold():
        message = default_message

    built_payload = {
        "plan_id": int(plan_meta["id"]),
        "status": status,
        "status_label": curriculum_plan_status_label(status),
        "message": message,
        "title": str(plan_meta.get("title") or payload.get("title") or "Черновик учебного плана"),
        "audience_level": str(plan_meta.get("audience_level") or payload.get("audience_level") or "Начальный"),
        "source_policy": str(plan_meta.get("source_policy") or payload.get("source_policy") or "accepted_only"),
        "summary": {
            "blocks": len(block_payloads),
            "projects": len(rows),
            "total_hours": int(total_hours) if isfinite(total_hours) else 0,
            "total_days": round(total_days, 2) if isfinite(total_days) else 0.0,
            "total_xp": int(total_xp),
        },
        "rows": rows,
        "row_count": len(rows),
        "blocks": block_payloads,
        "csv_primary_header": payload.get("csv_primary_header") or CSV_PRIMARY_HEADER,
        "csv_secondary_header": payload.get("csv_secondary_header") or CSV_SECONDARY_HEADER,
        "report": payload.get("report") if isinstance(payload.get("report"), dict) else {"coverage_ok": False, "order_violations": []},
    }
    return built_payload


def get_curriculum_plan(conn: sqlite3.Connection, plan_id: int) -> dict[str, object] | None:
    if not table_exists(conn, "curriculum_plan"):
        return None
    row = conn.execute(
        """
        SELECT
            cp.*,
            pb.role AS brief_role,
            pb.seniority AS brief_seniority,
            pb.domain AS brief_domain,
            (
                SELECT ij.id
                FROM intake_job ij
                WHERE ij.status = 'succeeded'
                  AND json_valid(ij.result_payload)
                  AND json_extract(ij.result_payload, '$.brief_id') = cp.brief_id
                ORDER BY ij.created_at DESC
                LIMIT 1
            ) AS latest_job_id
        FROM curriculum_plan cp
        LEFT JOIN profile_brief pb ON pb.id = cp.brief_id
        WHERE cp.id = ?
        """,
        (plan_id,),
    ).fetchone()
    if not row:
        return None
    plan_meta = dict(row)
    row_records = load_curriculum_plan_rows(conn, plan_id)
    plan_payload = build_curriculum_plan_payload_from_rows(plan_meta, row_records)
    plan_payload.update(
        {
            "id": int(plan_meta["id"]),
            "brief_id": plan_meta.get("brief_id"),
            "updated_at": plan_meta.get("updated_at"),
            "created_at": plan_meta.get("created_at"),
            "latest_job_id": plan_meta.get("latest_job_id"),
            "brief_role": plan_meta.get("brief_role"),
            "brief_seniority": plan_meta.get("brief_seniority"),
            "brief_domain": plan_meta.get("brief_domain"),
        }
    )
    return plan_payload


def list_curriculum_plans(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, object]]:
    if not table_exists(conn, "curriculum_plan"):
        return []
    rows = conn.execute(
        """
        SELECT
            cp.id,
            cp.brief_id,
            cp.status,
            cp.title,
            cp.audience_level,
            cp.total_blocks,
            cp.total_projects,
            cp.total_hours,
            cp.total_days,
            cp.total_xp,
            cp.updated_at,
            pb.role AS brief_role,
            pb.domain AS brief_domain,
            (
                SELECT ij.id
                FROM intake_job ij
                WHERE ij.status = 'succeeded'
                  AND json_valid(ij.result_payload)
                  AND json_extract(ij.result_payload, '$.brief_id') = cp.brief_id
                ORDER BY ij.created_at DESC
                LIMIT 1
            ) AS latest_job_id
        FROM curriculum_plan cp
        LEFT JOIN profile_brief pb ON pb.id = cp.brief_id
        ORDER BY cp.updated_at DESC, cp.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["status_label"] = curriculum_plan_status_label(str(item.get("status")))
        items.append(item)
    return items


def sync_curriculum_plan_payload(conn: sqlite3.Connection, plan_id: int) -> dict[str, object] | None:
    plan_payload = get_curriculum_plan(conn, plan_id)
    if not plan_payload:
        return None
    summary = plan_payload.get("summary") if isinstance(plan_payload.get("summary"), dict) else {}
    conn.execute(
        """
        UPDATE curriculum_plan
        SET status = ?,
            title = ?,
            audience_level = ?,
            total_blocks = ?,
            total_projects = ?,
            total_hours = ?,
            total_days = ?,
            total_xp = ?,
            payload_json = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            str(plan_payload.get("status") or "draft"),
            plan_payload.get("title"),
            plan_payload.get("audience_level"),
            int(summary.get("blocks", 0) or 0),
            int(summary.get("projects", 0) or 0),
            float(summary.get("total_hours", 0) or 0),
            float(summary.get("total_days", 0) or 0),
            int(summary.get("total_xp", 0) or 0),
            json.dumps(plan_payload, ensure_ascii=False),
            plan_id,
        ),
    )
    conn.commit()
    brief_id = plan_payload.get("brief_id")
    if isinstance(brief_id, int):
        update_jobs_curriculum_plan_payload(
            conn,
            brief_id,
            plan_payload,
            persisted_update={"curriculum_plan_rows": int(plan_payload.get("row_count", 0) or 0)},
        )
    return get_curriculum_plan(conn, plan_id)


def create_curriculum_plan_row(conn: sqlite3.Connection, plan_id: int) -> int:
    plan = get_curriculum_plan(conn, plan_id)
    if not plan:
        raise ValueError("Curriculum plan not found")
    existing_rows = plan.get("rows") if isinstance(plan.get("rows"), list) else []
    next_row_number = max((int(row.get("row_number", 0) or 0) for row in existing_rows), default=0) + 1
    next_block_index = max((int(row.get("block_index", 0) or 0) for row in existing_rows), default=0) or 1
    next_project_index = max((int(row.get("project_index_in_block", 0) or 0) for row in existing_rows if int(row.get("block_index", 0) or 0) == next_block_index), default=0) + 1
    cur = conn.execute(
        """
        INSERT INTO curriculum_plan_row(
            plan_id, block_index, row_number, project_index_in_block, block_title, block_goal,
            project_name, project_summary, learning_outcomes, skills_list, audience_level,
            required_tools, storytelling, delivery_format, group_size, effort_hours, effort_days,
            cumulative_days, xp, platform_project_name, artifact_links
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            next_block_index,
            next_row_number,
            next_project_index,
            f"Блок {next_block_index}",
            "",
            f"Новый проект {next_row_number}",
            "",
            "",
            "",
            plan.get("audience_level", "Начальный"),
            "",
            "",
            "индивидуальный",
            "",
            0.0,
            0.0,
            0.0,
            0,
            f"UP_{next_block_index}_{next_project_index}_{slugify(f'Новый проект {next_row_number}')}",
            "",
        ),
    )
    conn.commit()
    sync_curriculum_plan_payload(conn, plan_id)
    return int(cur.lastrowid)


def get_curriculum_plan_row(conn: sqlite3.Connection, plan_id: int, row_id: int) -> dict[str, object] | None:
    if not table_exists(conn, "curriculum_plan_row"):
        return None
    row = conn.execute(
        "SELECT * FROM curriculum_plan_row WHERE id = ? AND plan_id = ?",
        (row_id, plan_id),
    ).fetchone()
    return dict(row) if row else None


def parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.strip().replace(",", ".")
    if not cleaned:
        return None
    return float(cleaned)


def parse_optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return int(cleaned)


def update_curriculum_plan_row(conn: sqlite3.Connection, plan_id: int, row_id: int, form_data: dict[str, str]) -> dict[str, object]:
    row = get_curriculum_plan_row(conn, plan_id, row_id)
    if not row:
        raise ValueError("Curriculum plan row not found")
    conn.execute(
        """
        UPDATE curriculum_plan_row
        SET block_index = ?,
            row_number = ?,
            project_index_in_block = ?,
            block_title = ?,
            block_goal = ?,
            project_name = ?,
            project_summary = ?,
            learning_outcomes = ?,
            skills_list = ?,
            audience_level = ?,
            required_tools = ?,
            storytelling = ?,
            delivery_format = ?,
            group_size = ?,
            effort_hours = ?,
            effort_days = ?,
            cumulative_days = ?,
            xp = ?,
            platform_project_name = ?,
            artifact_links = ?
        WHERE id = ? AND plan_id = ?
        """,
        (
            parse_optional_int(form_data.get("block_index")) or 1,
            parse_optional_int(form_data.get("row_number")) or 1,
            parse_optional_int(form_data.get("project_index_in_block")) or 1,
            form_data.get("block_title", "").strip(),
            form_data.get("block_goal", "").strip(),
            form_data.get("project_name", "").strip(),
            form_data.get("project_summary", "").strip(),
            form_data.get("learning_outcomes", "").strip(),
            form_data.get("skills_list", "").strip(),
            form_data.get("audience_level", "").strip(),
            form_data.get("required_tools", "").strip(),
            form_data.get("storytelling", "").strip(),
            form_data.get("delivery_format", "").strip(),
            form_data.get("group_size", "").strip(),
            parse_optional_float(form_data.get("effort_hours")) or 0.0,
            parse_optional_float(form_data.get("effort_days")) or 0.0,
            parse_optional_float(form_data.get("cumulative_days")) or 0.0,
            parse_optional_int(form_data.get("xp")) or 0,
            form_data.get("platform_project_name", "").strip(),
            form_data.get("artifact_links", "").strip(),
            row_id,
            plan_id,
        ),
    )
    conn.commit()
    sync_curriculum_plan_payload(conn, plan_id)
    updated_row = get_curriculum_plan_row(conn, plan_id, row_id)
    if not updated_row:
        raise ValueError("Curriculum plan row not found after update")
    return updated_row


def delete_curriculum_plan_row(conn: sqlite3.Connection, plan_id: int, row_id: int) -> None:
    conn.execute("DELETE FROM curriculum_plan_row WHERE id = ? AND plan_id = ?", (row_id, plan_id))
    conn.commit()
    sync_curriculum_plan_payload(conn, plan_id)


def list_target_groups(conn: sqlite3.Connection) -> list[dict[str, object]]:
    return fetch_all(
        conn,
        """
        SELECT
            sg.id,
            sg.name,
            sg.code,
            sg.sort_order,
            sg.status,
            COUNT(DISTINCT s.id) AS skill_count,
            COUNT(DISTINCT i.id) AS indicator_count,
            (
                SELECT COUNT(*)
                FROM skill s_all
                WHERE s_all.group_id = sg.id
            ) AS total_skill_count,
            (
                SELECT COUNT(*)
                FROM indicator i_all
                JOIN skill s_all2 ON s_all2.id = i_all.skill_id
                WHERE s_all2.group_id = sg.id
            ) AS total_indicator_count
        FROM skill_group sg
        LEFT JOIN skill s ON s.group_id = sg.id AND s.is_active = 1
        LEFT JOIN indicator i ON i.skill_id = s.id AND i.is_active = 1
        WHERE sg.status != 'deprecated'
        GROUP BY sg.id, sg.name, sg.code, sg.sort_order, sg.status
        ORDER BY sg.sort_order, sg.name
        """,
    )


def get_target_group(conn: sqlite3.Connection, group_id: int) -> dict[str, object] | None:
    return fetch_one(
        conn,
        """
        SELECT
            sg.id,
            sg.name,
            sg.code,
            sg.sort_order,
            sg.status,
            COUNT(DISTINCT s.id) AS skill_count,
            COUNT(DISTINCT i.id) AS indicator_count,
            (
                SELECT COUNT(*)
                FROM skill s_all
                WHERE s_all.group_id = sg.id
            ) AS total_skill_count,
            (
                SELECT COUNT(*)
                FROM indicator i_all
                JOIN skill s_all2 ON s_all2.id = i_all.skill_id
                WHERE s_all2.group_id = sg.id
            ) AS total_indicator_count
        FROM skill_group sg
        LEFT JOIN skill s ON s.group_id = sg.id AND s.is_active = 1
        LEFT JOIN indicator i ON i.skill_id = s.id AND i.is_active = 1
        WHERE sg.id = ?
        GROUP BY sg.id, sg.name, sg.code, sg.sort_order, sg.status
        """,
        (group_id,),
    )


def list_target_group_skills(conn: sqlite3.Connection, group_id: int) -> list[dict[str, object]]:
    return fetch_all(
        conn,
        """
        SELECT
            s.id,
            s.name,
            s.code,
            s.sort_order,
            s.complexity_summary,
            s.source_scale_title,
            s.source_skill_name,
            s.resolution_status,
            s.match_note,
            s.is_active,
            COUNT(i.id) AS indicator_count,
            (
                SELECT COUNT(*)
                FROM indicator i_all
                WHERE i_all.skill_id = s.id
            ) AS total_indicator_count
        FROM skill s
        LEFT JOIN indicator i ON i.skill_id = s.id AND i.is_active = 1
        WHERE s.group_id = ?
          AND s.is_active = 1
        GROUP BY s.id, s.name, s.code, s.sort_order, s.complexity_summary, s.source_scale_title, s.source_skill_name, s.resolution_status, s.match_note, s.is_active
        ORDER BY s.is_active DESC, s.sort_order, s.name, s.id
        """,
        (group_id,),
    )


def get_target_skill(conn: sqlite3.Connection, skill_id: int) -> dict[str, object] | None:
    return fetch_one(
        conn,
        """
        SELECT
            s.id,
            s.group_id,
            s.name,
            s.code,
            s.normalized_name,
            s.sort_order,
            s.complexity_min_band,
            s.complexity_max_band,
            s.complexity_summary,
            s.source_scale_title,
            s.description,
            s.source_skill_name,
            s.resolution_status,
            s.match_note,
            s.is_active,
            (
                SELECT COUNT(*)
                FROM indicator i_all
                WHERE i_all.skill_id = s.id
            ) AS total_indicator_count,
            sg.name AS group_name
        FROM skill s
        JOIN skill_group sg ON sg.id = s.group_id
        WHERE s.id = ?
        """,
        (skill_id,),
    )


def get_target_indicator(conn: sqlite3.Connection, indicator_id: int) -> dict[str, object] | None:
    return fetch_one(
        conn,
        """
        SELECT
            id,
            skill_id,
            indicator_type,
            text,
            sort_order,
            complexity_band,
            complexity_label,
            complexity_sort_order,
            is_active,
            source_profile_name,
            source_scale_title
        FROM indicator
        WHERE id = ?
        """,
        (indicator_id,),
    )


def list_target_indicators(conn: sqlite3.Connection, skill_id: int) -> list[dict[str, object]]:
    return fetch_all(
        conn,
        """
        SELECT
            id,
            indicator_type,
            text,
            sort_order,
            complexity_band,
            complexity_label,
            complexity_sort_order,
            is_active,
            source_profile_name,
            source_scale_title
        FROM indicator
        WHERE skill_id = ?
          AND is_active = 1
        ORDER BY is_active DESC, sort_order, id
        """,
        (skill_id,),
    )


def list_archived_groups(conn: sqlite3.Connection, query: str = "") -> list[dict[str, object]]:
    params: list[object] = []
    where_parts = ["sg.status = 'deprecated'"]
    if query:
        needle = normalize_search_text(query)
        where_parts.append("(instr(search_norm(sg.name), ?) > 0 OR instr(search_norm(sg.code), ?) > 0)")
        params.extend([needle, needle])
    sql = f"""
        SELECT
            sg.id,
            sg.name,
            sg.code,
            sg.sort_order,
            sg.status,
            COUNT(DISTINCT s.id) AS total_skill_count,
            COUNT(DISTINCT i.id) AS total_indicator_count
        FROM skill_group sg
        LEFT JOIN skill s ON s.group_id = sg.id
        LEFT JOIN indicator i ON i.skill_id = s.id
        WHERE {' AND '.join(where_parts)}
        GROUP BY sg.id, sg.name, sg.code, sg.sort_order, sg.status
        ORDER BY sg.sort_order, sg.name
    """
    return fetch_all(conn, sql, tuple(params))


def list_archived_skills(conn: sqlite3.Connection, query: str = "") -> list[dict[str, object]]:
    params: list[object] = []
    where_parts = ["s.is_active = 0"]
    if query:
        needle = normalize_search_text(query)
        where_parts.append(
            """
            (
                instr(search_norm(s.name), ?) > 0
                OR instr(search_norm(s.normalized_name), ?) > 0
                OR instr(search_norm(COALESCE(s.source_skill_name, '')), ?) > 0
                OR instr(search_norm(sg.name), ?) > 0
            )
            """
        )
        params.extend([needle, needle, needle, needle])
    sql = f"""
        SELECT
            s.id,
            s.group_id,
            s.name,
            s.sort_order,
            s.complexity_summary,
            s.source_scale_title,
            s.source_skill_name,
            s.resolution_status,
            sg.name AS group_name,
            COUNT(i.id) AS total_indicator_count
        FROM skill s
        JOIN skill_group sg ON sg.id = s.group_id
        LEFT JOIN indicator i ON i.skill_id = s.id
        WHERE {' AND '.join(where_parts)}
        GROUP BY s.id, s.group_id, s.name, s.sort_order, s.complexity_summary, s.source_scale_title, s.source_skill_name, s.resolution_status, sg.name
        ORDER BY sg.sort_order, s.sort_order, s.name, s.id
    """
    return fetch_all(conn, sql, tuple(params))


def list_archived_indicators(conn: sqlite3.Connection, query: str = "") -> list[dict[str, object]]:
    params: list[object] = []
    where_parts = ["i.is_active = 0"]
    if query:
        needle = normalize_search_text(query)
        where_parts.append(
            """
            (
                instr(search_norm(i.text), ?) > 0
                OR instr(search_norm(i.normalized_text), ?) > 0
                OR instr(search_norm(i.indicator_type), ?) > 0
                OR instr(search_norm(s.name), ?) > 0
                OR instr(search_norm(s.normalized_name), ?) > 0
                OR instr(search_norm(sg.name), ?) > 0
                OR instr(search_norm(COALESCE(i.source_profile_name, '')), ?) > 0
            )
            """
        )
        params.extend([needle, needle, needle, needle, needle, needle, needle])
    sql = f"""
        SELECT
            i.id,
            i.skill_id,
            i.indicator_type,
            i.text,
            i.sort_order,
            i.complexity_band,
            i.complexity_label,
            i.source_profile_name,
            i.source_scale_title,
            s.name AS skill_name,
            sg.id AS group_id,
            sg.name AS group_name
        FROM indicator i
        JOIN skill s ON s.id = i.skill_id
        JOIN skill_group sg ON sg.id = s.group_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY sg.sort_order, s.sort_order, i.sort_order, i.id
    """
    return fetch_all(conn, sql, tuple(params))


def create_target_group(conn: sqlite3.Connection, name: str, sort_order: int, status: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO skill_group (code, name, sort_order, status, source, updated_at)
        VALUES (?, ?, ?, ?, 'manual', ?)
        """,
        (f"group-{slugify(name)}", name.strip(), sort_order, status, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_target_group(conn: sqlite3.Connection, group_id: int, name: str, sort_order: int, status: str) -> None:
    conn.execute(
        """
        UPDATE skill_group
        SET code = ?, name = ?, sort_order = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (f"group-{slugify(name)}", name.strip(), sort_order, status, datetime.now(UTC).isoformat(), group_id),
    )
    conn.commit()


def remove_target_group(conn: sqlite3.Connection, group_id: int) -> str:
    row = fetch_one(
        conn,
        """
        SELECT
            sg.id,
            COALESCE((
                SELECT COUNT(*)
                FROM skill s_all
                WHERE s_all.group_id = sg.id
            ), 0) AS total_skill_count
        FROM skill_group sg
        WHERE sg.id = ?
        """,
        (group_id,),
    )
    if not row:
        return "missing"
    if row["total_skill_count"]:
        conn.execute(
            """
            UPDATE skill_group
            SET status = 'deprecated',
                updated_at = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), group_id),
        )
        conn.commit()
        return "archived"

    conn.execute("DELETE FROM skill_group WHERE id = ?", (group_id,))
    conn.commit()
    return "deleted"


def restore_target_group(conn: sqlite3.Connection, group_id: int) -> str:
    group = get_target_group(conn, group_id)
    if not group and not fetch_one(conn, "SELECT id FROM skill_group WHERE id = ?", (group_id,)):
        return "missing"
    conn.execute(
        """
        UPDATE skill_group
        SET status = 'active',
            updated_at = ?
        WHERE id = ?
        """,
        (datetime.now(UTC).isoformat(), group_id),
    )
    conn.commit()
    return "restored"


def create_target_skill(
    conn: sqlite3.Connection,
    group_id: int,
    name: str,
    sort_order: int,
    description: str,
    source_skill_name: str,
    resolution_status: str,
    match_note: str,
    is_active: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO skill (
            group_id,
            code,
            name,
            normalized_name,
            sort_order,
            description,
            source_skill_name,
            resolution_status,
            match_note,
            is_active,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_id,
            f"skill-{slugify(name)}-{group_id}",
            name.strip(),
            name.casefold().replace("ё", "е").strip(),
            sort_order,
            description.strip() or None,
            source_skill_name.strip() or None,
            resolution_status,
            match_note.strip() or None,
            is_active,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_target_skill(
    conn: sqlite3.Connection,
    skill_id: int,
    name: str,
    sort_order: int,
    description: str,
    source_skill_name: str,
    resolution_status: str,
    match_note: str,
    is_active: int,
) -> None:
    skill = get_target_skill(conn, skill_id)
    if not skill:
        return
    conn.execute(
        """
        UPDATE skill
        SET code = ?,
            name = ?,
            normalized_name = ?,
            sort_order = ?,
            description = ?,
            source_skill_name = ?,
            resolution_status = ?,
            match_note = ?,
            is_active = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            f"skill-{slugify(name)}-{skill['group_id']}",
            name.strip(),
            name.casefold().replace("ё", "е").strip(),
            sort_order,
            description.strip() or None,
            source_skill_name.strip() or None,
            resolution_status,
            match_note.strip() or None,
            is_active,
            datetime.now(UTC).isoformat(),
            skill_id,
        ),
    )
    conn.commit()


def remove_target_skill(conn: sqlite3.Connection, skill_id: int) -> str:
    skill = get_target_skill(conn, skill_id)
    if not skill:
        return "missing"

    indicator_count = conn.execute("SELECT COUNT(*) FROM indicator WHERE skill_id = ?", (skill_id,)).fetchone()[0]
    if indicator_count:
        conn.execute(
            """
            UPDATE skill
            SET is_active = 0,
                updated_at = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), skill_id),
        )
        conn.commit()
        return "archived"

    conn.execute("DELETE FROM skill WHERE id = ?", (skill_id,))
    conn.commit()
    return "deleted"


def restore_target_skill(conn: sqlite3.Connection, skill_id: int) -> str:
    skill = get_target_skill(conn, skill_id)
    if not skill:
        return "missing"
    conn.execute(
        """
        UPDATE skill
        SET is_active = 1,
            updated_at = ?
        WHERE id = ?
        """,
        (datetime.now(UTC).isoformat(), skill_id),
    )
    conn.execute(
        """
        UPDATE skill_group
        SET status = 'active',
            updated_at = ?
        WHERE id = ?
        """,
        (datetime.now(UTC).isoformat(), skill["group_id"]),
    )
    refresh_target_skill_complexity(conn, skill_id, commit=False)
    conn.commit()
    return "restored"


def create_target_indicator(
    conn: sqlite3.Connection,
    skill_id: int,
    indicator_type: str,
    text: str,
    sort_order: int,
    complexity_band: str,
    is_active: int,
) -> int:
    normalized_band = complexity_band.strip()
    complexity_label = complexity_label_for_band(normalized_band) if normalized_band else None
    complexity_sort_order = COMPLEXITY_ORDER.get(normalized_band) if normalized_band else None
    cursor = conn.execute(
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
            source_profile_name,
            source_scale_title,
            is_active,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            skill_id,
            indicator_type.strip(),
            text.strip(),
            text.casefold().replace("ё", "е").strip(),
            sort_order,
            normalized_band or None,
            complexity_label,
            complexity_sort_order,
            "manual",
            None,
            is_active,
            datetime.now(UTC).isoformat(),
        ),
    )
    refresh_target_skill_complexity(conn, skill_id, commit=False)
    conn.commit()
    return int(cursor.lastrowid)


def update_target_indicator(
    conn: sqlite3.Connection,
    indicator_id: int,
    indicator_type: str,
    text: str,
    sort_order: int,
    complexity_band: str,
    is_active: int,
) -> None:
    row = conn.execute("SELECT skill_id FROM indicator WHERE id = ?", (indicator_id,)).fetchone()
    if not row:
        return
    normalized_band = complexity_band.strip()
    complexity_label = complexity_label_for_band(normalized_band) if normalized_band else None
    complexity_sort_order = COMPLEXITY_ORDER.get(normalized_band) if normalized_band else None
    conn.execute(
        """
        UPDATE indicator
        SET indicator_type = ?,
            text = ?,
            normalized_text = ?,
            sort_order = ?,
            complexity_band = ?,
            complexity_label = ?,
            complexity_sort_order = ?,
            is_active = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            indicator_type.strip(),
            text.strip(),
            text.casefold().replace("ё", "е").strip(),
            sort_order,
            normalized_band or None,
            complexity_label,
            complexity_sort_order,
            is_active,
            datetime.now(UTC).isoformat(),
            indicator_id,
        ),
    )
    refresh_target_skill_complexity(conn, row["skill_id"], commit=False)
    conn.commit()


def remove_target_indicator(conn: sqlite3.Connection, indicator_id: int) -> str:
    indicator = get_target_indicator(conn, indicator_id)
    if not indicator:
        return "missing"

    skill_id = int(indicator["skill_id"])
    if indicator.get("source_profile_name") == "manual":
        conn.execute("DELETE FROM indicator WHERE id = ?", (indicator_id,))
        refresh_target_skill_complexity(conn, skill_id, commit=False)
        conn.commit()
        return "deleted"

    conn.execute(
        """
        UPDATE indicator
        SET is_active = 0,
            updated_at = ?
        WHERE id = ?
        """,
        (datetime.now(UTC).isoformat(), indicator_id),
    )
    refresh_target_skill_complexity(conn, skill_id, commit=False)
    conn.commit()
    return "archived"


def restore_target_indicator(conn: sqlite3.Connection, indicator_id: int) -> str:
    indicator = get_target_indicator(conn, indicator_id)
    if not indicator:
        return "missing"

    skill = get_target_skill(conn, int(indicator["skill_id"]))
    conn.execute(
        """
        UPDATE indicator
        SET is_active = 1,
            updated_at = ?
        WHERE id = ?
        """,
        (datetime.now(UTC).isoformat(), indicator_id),
    )
    if skill:
        conn.execute(
            """
            UPDATE skill
            SET is_active = 1,
                updated_at = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), skill["id"]),
        )
        conn.execute(
            """
            UPDATE skill_group
            SET status = 'active',
                updated_at = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), skill["group_id"]),
        )
        refresh_target_skill_complexity(conn, skill["id"], commit=False)
    conn.commit()
    return "restored"


def resolve_directory_profile(conn: sqlite3.Connection) -> dict[str, object] | None:
    comparison_report = load_summary(DEFAULT_COMPARE_REPORT)
    preferred_name = comparison_report.get("profile_name") if isinstance(comparison_report, dict) else None
    if preferred_name:
        preferred = fetch_one(conn, "SELECT id, name, source_kind FROM profile WHERE name = ?", (preferred_name,))
        if preferred:
            return preferred

    return fetch_one(
        conn,
        """
        SELECT id, name, source_kind
        FROM profile
        ORDER BY CASE WHEN name LIKE '%Java%' THEN 0 ELSE 1 END, name
        LIMIT 1
        """,
    )


def has_directory_hierarchy(conn: sqlite3.Connection) -> bool:
    if not table_exists(conn, "typed_competency") or not table_exists(conn, "typed_competency_skill"):
        return False
    row = conn.execute("SELECT COUNT(*) AS cnt FROM typed_competency").fetchone()
    return bool(row and row["cnt"])


def list_directory_hierarchy(
    conn: sqlite3.Connection,
    query: str,
    scope: str,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    profile = resolve_directory_profile(conn)
    typed_competencies = fetch_all(
        conn,
        """
        SELECT id, name, sort_order
        FROM typed_competency
        WHERE status = 'active'
        ORDER BY sort_order, name
        """,
    )
    typed_skills = fetch_all(
        conn,
        """
        SELECT
            tcs.id,
            tcs.typed_competency_id,
            tcs.source_skill_name,
            tcs.sort_order,
            tcs.resolution_status,
            tcs.match_note,
            s.id AS skill_id,
            s.canonical_name
        FROM typed_competency_skill tcs
        LEFT JOIN skill s ON s.id = tcs.skill_id
        WHERE tcs.source = 'live_snapshot'
        ORDER BY tcs.typed_competency_id, tcs.sort_order
        """,
    )

    indicator_map: dict[int, list[dict[str, object]]] = {}
    if profile:
        indicator_rows = fetch_all(
            conn,
            """
            SELECT
                s.id AS skill_id,
                COALESCE(d.title, 'Не указано') AS dimension_title,
                ilc.raw_value
            FROM profile_competency pc
            JOIN competency_skill cs ON cs.profile_competency_id = pc.id
            JOIN skill s ON s.id = cs.skill_id
            LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
            LEFT JOIN dimension d ON d.id = ir.dimension_id
            LEFT JOIN indicator_level_cell ilc ON ilc.indicator_row_id = ir.id
            WHERE pc.profile_id = ?
              AND COALESCE(TRIM(ilc.raw_value), '') <> ''
            ORDER BY
                s.canonical_name,
                CASE COALESCE(d.title, '')
                    WHEN 'Знает' THEN 1
                    WHEN 'Умеет' THEN 2
                    ELSE 3
                END,
                ilc.sort_order,
                ilc.raw_value
            """,
            (profile["id"],),
        )
        seen_by_skill: dict[int, set[str]] = {}
        for row in indicator_rows:
            skill_id = row["skill_id"]
            full_text = f"{row['dimension_title']}: {row['raw_value']}".strip()
            seen_by_skill.setdefault(skill_id, set())
            if full_text in seen_by_skill[skill_id]:
                continue
            seen_by_skill[skill_id].add(full_text)
            indicator_map.setdefault(skill_id, []).append(
                {
                    "dimension": row["dimension_title"],
                    "text": row["raw_value"],
                    "full_text": full_text,
                }
            )

    query_folded = query.casefold()
    groups: list[dict[str, object]] = []
    skill_rows_by_group: dict[int, list[dict[str, object]]] = {}
    for row in typed_skills:
        skill_rows_by_group.setdefault(row["typed_competency_id"], []).append(row)

    resolution_labels = {
        "matched": "совпало",
        "alias": "сопоставлено по alias",
        "manual": "сопоставлено вручную",
        "fuzzy": "сопоставлено нечетко",
        "missing": "нет локального skill",
    }

    for typed_competency in typed_competencies:
        group_name = typed_competency["name"]
        group_matches = bool(query_folded) and query_folded in group_name.casefold()
        all_skills: list[dict[str, object]] = []

        for skill_row in skill_rows_by_group.get(typed_competency["id"], []):
            display_name = skill_row["canonical_name"] or skill_row["source_skill_name"]
            indicators = indicator_map.get(skill_row["skill_id"], []) if skill_row["skill_id"] else []
            skill_entry = {
                "id": skill_row["id"],
                "display_name": display_name,
                "source_name": skill_row["source_skill_name"],
                "resolved_name": skill_row["canonical_name"],
                "resolution_status": skill_row["resolution_status"],
                "resolution_label": resolution_labels.get(skill_row["resolution_status"], skill_row["resolution_status"]),
                "match_note": skill_row["match_note"],
                "indicator_count": len(indicators),
                "indicators": indicators,
            }
            all_skills.append(skill_entry)

        if not query_folded:
            matched_skills = all_skills
        elif scope == "competencies":
            matched_skills = all_skills if group_matches else []
        else:
            matched_skills = []
            for skill_entry in all_skills:
                skill_matches = query_folded in skill_entry["display_name"].casefold() or query_folded in skill_entry["source_name"].casefold()
                indicator_matches = any(query_folded in indicator["full_text"].casefold() for indicator in skill_entry["indicators"])
                if scope == "skills" and skill_matches:
                    matched_skills.append(skill_entry)
                elif scope == "indicators" and indicator_matches:
                    matched_skills.append(skill_entry)
                elif scope == "all" and (group_matches or skill_matches or indicator_matches):
                    matched_skills.append(skill_entry)
            if scope == "all" and group_matches:
                matched_skills = all_skills

        if not matched_skills:
            continue

        groups.append(
            {
                "id": typed_competency["id"],
                "name": group_name,
                "skill_count": len(matched_skills),
                "indicator_count": sum(skill["indicator_count"] for skill in matched_skills),
                "skills": matched_skills,
                "open_on_load": bool(query_folded),
            }
        )

    return groups, profile


def list_competencies(conn: sqlite3.Connection, query: str, scope: str) -> list[dict[str, object]]:
    params: list[object] = []
    where_parts: list[str] = []
    if query:
        like = f"%{query}%"
        if scope == "competencies":
            where_parts.append("(c.title LIKE ? OR COALESCE(c.description, '') LIKE ?)")
            params.extend([like, like])
        elif scope == "skills":
            where_parts.append(
                """EXISTS (
                    SELECT 1
                    FROM profile_competency pc2
                    JOIN competency_skill cs2 ON cs2.profile_competency_id = pc2.id
                    JOIN skill s2 ON s2.id = cs2.skill_id
                    WHERE pc2.competency_id = c.id
                      AND s2.canonical_name LIKE ?
                )"""
            )
            params.append(like)
        elif scope == "indicators":
            where_parts.append(
                """EXISTS (
                    SELECT 1
                    FROM profile_competency pc2
                    JOIN competency_skill cs2 ON cs2.profile_competency_id = pc2.id
                    JOIN indicator_row ir2 ON ir2.competency_skill_id = cs2.id
                    WHERE pc2.competency_id = c.id
                      AND COALESCE(ir2.base_text, '') LIKE ?
                )"""
            )
            params.append(like)
        else:
            where_parts.append(
                """(
                    c.title LIKE ?
                    OR COALESCE(c.description, '') LIKE ?
                    OR EXISTS (
                        SELECT 1
                        FROM profile_competency pc2
                        JOIN competency_skill cs2 ON cs2.profile_competency_id = pc2.id
                        JOIN skill s2 ON s2.id = cs2.skill_id
                        WHERE pc2.competency_id = c.id
                          AND s2.canonical_name LIKE ?
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM profile_competency pc3
                        JOIN competency_skill cs3 ON cs3.profile_competency_id = pc3.id
                        JOIN indicator_row ir3 ON ir3.competency_skill_id = cs3.id
                        WHERE pc3.competency_id = c.id
                          AND COALESCE(ir3.base_text, '') LIKE ?
                    )
                )"""
            )
            params.extend([like, like, like, like])

    sql = f"""
        SELECT
            c.id,
            c.title,
            c.description,
            c.status,
            COUNT(DISTINCT pc.profile_id) AS profile_count,
            COUNT(DISTINCT cs.skill_id) AS skill_count,
            COUNT(DISTINCT ir.id) AS indicator_count
        FROM competency c
        LEFT JOIN profile_competency pc ON pc.competency_id = c.id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        {"WHERE " + " AND ".join(where_parts) if where_parts else ""}
        GROUP BY c.id, c.title, c.description, c.status
        ORDER BY c.title
    """
    return fetch_all(conn, sql, tuple(params))


def get_competency(conn: sqlite3.Connection, competency_id: int) -> dict[str, object] | None:
    return fetch_one(
        conn,
        """
        SELECT
            c.id,
            c.title,
            c.description,
            c.status,
            COUNT(DISTINCT pc.profile_id) AS profile_count,
            COUNT(DISTINCT cs.skill_id) AS skill_count,
            COUNT(DISTINCT ir.id) AS indicator_count
        FROM competency c
        LEFT JOIN profile_competency pc ON pc.competency_id = c.id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        WHERE c.id = ?
        GROUP BY c.id, c.title, c.description, c.status
        """,
        (competency_id,),
    )


def get_competency_skills(conn: sqlite3.Connection, competency_id: int) -> list[dict[str, object]]:
    rows = fetch_all(
        conn,
        """
        SELECT
            s.id AS skill_id,
            s.canonical_name,
            s.skill_type,
            COUNT(DISTINCT pc.profile_id) AS profile_count,
            COUNT(DISTINCT ir.id) AS indicator_count,
            GROUP_CONCAT(DISTINCT p.name) AS profile_names
        FROM profile_competency pc
        JOIN profile p ON p.id = pc.profile_id
        JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        JOIN skill s ON s.id = cs.skill_id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        WHERE pc.competency_id = ?
        GROUP BY s.id, s.canonical_name, s.skill_type
        ORDER BY s.canonical_name
        """,
        (competency_id,),
    )

    indicator_rows = fetch_all(
        conn,
        """
        SELECT
            s.id AS skill_id,
            s.canonical_name,
            d.title AS dimension_title,
            COALESCE(ir.base_text, '') AS indicator_text,
            ilc.raw_level_label,
            ilc.raw_value,
            ilc.value_kind
        FROM profile_competency pc
        JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        JOIN skill s ON s.id = cs.skill_id
        JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        LEFT JOIN dimension d ON d.id = ir.dimension_id
        LEFT JOIN indicator_level_cell ilc ON ilc.indicator_row_id = ir.id
        WHERE pc.competency_id = ?
        ORDER BY s.canonical_name, ir.id, ilc.sort_order
        """,
        (competency_id,),
    )

    skill_map: dict[int, dict[str, object]] = {row["skill_id"]: {**row, "indicators": []} for row in rows}
    indicator_map: dict[tuple[int, str, str], dict[str, object]] = {}
    for row in indicator_rows:
        skill = skill_map.get(row["skill_id"])
        if not skill:
            continue
        key = (row["skill_id"], row["dimension_title"] or "Не указано", row["indicator_text"])
        if key not in indicator_map:
            indicator_map[key] = {
                "dimension_title": row["dimension_title"] or "Не указано",
                "indicator_text": row["indicator_text"] or "[нет текста]",
                "levels": [],
            }
            skill["indicators"].append(indicator_map[key])
        if row["raw_level_label"]:
            indicator_map[key]["levels"].append(
                {
                    "label": row["raw_level_label"],
                    "value": row["raw_value"],
                    "kind": row["value_kind"],
                }
            )
    return list(skill_map.values())


def list_profiles(conn: sqlite3.Connection) -> list[dict[str, object]]:
    return fetch_all(
        conn,
        """
        SELECT
            p.id,
            p.name,
            p.source_kind,
            COUNT(DISTINCT pc.id) AS competency_count,
            COUNT(DISTINCT cs.id) AS skill_count,
            COUNT(DISTINCT ir.id) AS indicator_count,
            SUM(CASE WHEN pc.review_state = 'needs_review' THEN 1 ELSE 0 END) AS review_competencies
        FROM profile p
        LEFT JOIN profile_competency pc ON pc.profile_id = p.id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        GROUP BY p.id, p.name, p.source_kind
        ORDER BY p.name
        """
    )


def get_profile(conn: sqlite3.Connection, profile_id: int) -> dict[str, object] | None:
    return fetch_one(
        conn,
        """
        SELECT
            p.id,
            p.name,
            p.source_kind,
            COUNT(DISTINCT pc.id) AS competency_count,
            COUNT(DISTINCT cs.id) AS skill_count,
            COUNT(DISTINCT ir.id) AS indicator_count
        FROM profile p
        LEFT JOIN profile_competency pc ON pc.profile_id = p.id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        WHERE p.id = ?
        GROUP BY p.id, p.name, p.source_kind
        """,
        (profile_id,),
    )


def get_profile_tree(conn: sqlite3.Connection, profile_id: int) -> list[dict[str, object]]:
    rows = fetch_all(
        conn,
        """
        SELECT
            pc.id AS profile_competency_id,
            pc.sort_order AS competency_order,
            pc.description_in_source AS competency_description,
            pc.prerequisites_text,
            pc.review_state,
            c.title AS competency_title,
            ps.title AS scale_title,
            cs.id AS competency_skill_id,
            cs.skill_order,
            s.canonical_name,
            ir.id AS indicator_row_id,
            ir.base_text,
            ir.source_row_number,
            d.title AS dimension_title,
            ilc.raw_level_label,
            ilc.raw_value,
            ilc.value_kind
        FROM profile_competency pc
        JOIN competency c ON c.id = pc.competency_id
        LEFT JOIN proficiency_scale ps ON ps.id = pc.scale_id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        LEFT JOIN skill s ON s.id = cs.skill_id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        LEFT JOIN dimension d ON d.id = ir.dimension_id
        LEFT JOIN indicator_level_cell ilc ON ilc.indicator_row_id = ir.id
        WHERE pc.profile_id = ?
        ORDER BY pc.sort_order, cs.skill_order, ir.source_row_number, ilc.sort_order
        """,
        (profile_id,),
    )

    competencies: list[dict[str, object]] = []
    competency_map: dict[int, dict[str, object]] = {}
    skill_map: dict[int, dict[str, object]] = {}
    indicator_map: dict[int, dict[str, object]] = {}

    for row in rows:
        pc_id = row["profile_competency_id"]
        competency = competency_map.get(pc_id)
        if competency is None:
            competency = {
                "id": pc_id,
                "title": row["competency_title"],
                "description": row["competency_description"],
                "prerequisites": row["prerequisites_text"],
                "scale_title": row["scale_title"],
                "review_state": row["review_state"],
                "skills": [],
            }
            competency_map[pc_id] = competency
            competencies.append(competency)

        skill_id = row["competency_skill_id"]
        if skill_id is not None:
            skill = skill_map.get(skill_id)
            if skill is None:
                skill = {
                    "id": skill_id,
                    "name": row["canonical_name"],
                    "indicators": [],
                }
                skill_map[skill_id] = skill
                competency["skills"].append(skill)

            indicator_id = row["indicator_row_id"]
            if indicator_id is not None:
                indicator = indicator_map.get(indicator_id)
                if indicator is None:
                    indicator = {
                        "id": indicator_id,
                        "dimension_title": row["dimension_title"] or "Не указано",
                        "text": row["base_text"] or "[нет текста]",
                        "levels": [],
                    }
                    indicator_map[indicator_id] = indicator
                    skill["indicators"].append(indicator)
                if row["raw_level_label"]:
                    indicator["levels"].append(
                        {
                            "label": row["raw_level_label"],
                            "value": row["raw_value"],
                            "kind": row["value_kind"],
                        }
                    )
    return competencies


def list_reviews(
    conn: sqlite3.Connection,
    status_filter: str,
    severity_filter: str,
    reason_filter: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, str]]]:
    repair_intake_review_links(conn)
    params: list[object] = []
    where_parts: list[str] = []
    if status_filter != "all":
        where_parts.append("status = ?")
        params.append(status_filter)
    if severity_filter != "all":
        where_parts.append("severity = ?")
        params.append(severity_filter)
    if reason_filter != "all":
        where_parts.append("reason_code = ?")
        params.append(reason_filter)
    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    status_totals = fetch_all(
        conn,
        """
        SELECT status, COUNT(*) AS cnt
        FROM review_queue
        GROUP BY status
        ORDER BY CASE status
            WHEN 'open' THEN 1
            WHEN 'resolved' THEN 2
            WHEN 'ignored' THEN 3
            ELSE 4
        END
        """,
    )
    for item in status_totals:
        item["status_label"] = review_status_label(str(item["status"]))

    breakdown = fetch_all(
        conn,
        f"""
        SELECT reason_code, severity, COUNT(*) AS cnt
        FROM review_queue
        {where_clause}
        GROUP BY reason_code, severity
        ORDER BY cnt DESC, reason_code
        """,
        tuple(params),
    )
    for item in breakdown:
        item["reason_label"] = review_reason_label(str(item["reason_code"]))
        item["severity_label"] = review_severity_label(str(item["severity"]))

    items = fetch_all(
        conn,
        f"""
        SELECT id, entity_type, entity_id, source_ref, reason_code, severity, details, status, resolution_note, created_at, reviewed_at
        FROM review_queue
        {where_clause}
        ORDER BY
            CASE severity
                WHEN 'error' THEN 1
                WHEN 'warning' THEN 2
                ELSE 3
            END,
            created_at DESC
        LIMIT 500
        """,
        tuple(params),
    )
    for item in items:
        item["reason_label"] = review_reason_label(str(item["reason_code"]))
        item["severity_label"] = review_severity_label(str(item["severity"]))
        item["status_label"] = review_status_label(str(item["status"]))

    reason_options = [
        {"code": row["reason_code"], "label": review_reason_label(str(row["reason_code"]))}
        for row in conn.execute("SELECT DISTINCT reason_code FROM review_queue ORDER BY reason_code")
    ]
    return status_totals, breakdown, items, reason_options


def response(start_response, body: bytes, status: str = "200 OK", content_type: str = "text/html; charset=utf-8", headers: list[tuple[str, str]] | None = None):
    final_headers = [("Content-Type", content_type), ("Content-Length", str(len(body)))]
    if headers:
        final_headers.extend(headers)
    start_response(status, final_headers)
    return [body]


def html_response(start_response, html: str, status: str = "200 OK"):
    return response(start_response, html.encode("utf-8"), status=status)


def json_response(start_response, payload: dict[str, object], status: str = "200 OK"):
    return response(
        start_response,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        status=status,
        content_type="application/json; charset=utf-8",
    )


def redirect_response(start_response, location: str):
    return response(start_response, b"", status="302 Found", headers=[("Location", location)])


def not_found(start_response, text: str = "Not found"):
    return response(start_response, text.encode("utf-8"), status="404 Not Found", content_type="text/plain; charset=utf-8")


def create_app(db_path: Path, summary_path: Path, target_db_path: Path):
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    summary = load_summary(summary_path)

    def render(template_name: str, context: dict[str, object]) -> str:
        template = env.get_template(template_name)
        shared = {
            "nav": [
                {"label": "Справочник", "href": "/competencies"},
                {"label": "Скиллсеты", "href": "/profiles"},
                {"label": "Каталог DB", "href": "/catalog-admin/groups"},
                {"label": "Архив", "href": "/catalog-admin/archive"},
                {"label": "Проверка", "href": "/reviews"},
                {"label": "Бриф", "href": "/intake"},
                {"label": "УП", "href": "/up"},
            ],
            "complexity_options": COMPLEXITY_OPTIONS,
            "intake_progress_steps": INTAKE_PROGRESS_STEPS,
            "summary": summary,
            "request_path": context.get("request_path", "/"),
        }
        merged = {**shared, **context}
        return template.render(**merged)

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET").upper()
        query_params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)

        if path.startswith("/static/"):
            static_path = (STATIC_DIR / path.removeprefix("/static/")).resolve()
            if STATIC_DIR.resolve() not in static_path.parents and static_path != STATIC_DIR.resolve():
                return not_found(start_response)
            if not static_path.exists() or not static_path.is_file():
                return not_found(start_response)
            mime, _ = mimetypes.guess_type(static_path.name)
            return response(start_response, static_path.read_bytes(), content_type=mime or "application/octet-stream")

        if path == "/favicon.ico":
            return response(start_response, b"", status="204 No Content", content_type="image/x-icon")

        if path == "/":
            return redirect_response(start_response, "/competencies")

        if path == "/catalog-admin":
            return redirect_response(start_response, "/catalog-admin/groups")

        if path == "/intake/pick-file" and method == "POST":
            picker_result = open_native_brief_picker()
            status = "200 OK" if picker_result.get("ok") or picker_result.get("cancelled") else "500 Internal Server Error"
            return json_response(start_response, picker_result, status=status)

        if path == "/catalog-admin/archive" and method == "GET":
            target_conn = open_target_db(target_db_path)
            try:
                archive_query = query_params.get("q", [""])[-1].strip()
                archive_scope = query_params.get("scope", ["all"])[-1].strip() or "all"
                if archive_scope not in {"all", "groups", "skills", "indicators"}:
                    archive_scope = "all"

                groups = list_archived_groups(target_conn, archive_query) if archive_scope in {"all", "groups"} else []
                skills = list_archived_skills(target_conn, archive_query) if archive_scope in {"all", "skills"} else []
                indicators = list_archived_indicators(target_conn, archive_query) if archive_scope in {"all", "indicators"} else []
                html = render(
                    "catalog_admin_archive.html",
                    {
                        "title": "Архив каталога",
                        "archived_groups": groups,
                        "archived_skills": skills,
                        "archived_indicators": indicators,
                        "archive_query": archive_query,
                        "archive_scope": archive_scope,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                target_conn.close()

        if path == "/catalog-admin/archive" and method == "POST":
            target_conn = open_target_db(target_db_path)
            try:
                form_data = parse_post_data(environ)
                action = form_data.get("action", "")
                if action == "restore_group":
                    restore_target_group(target_conn, int(form_data["group_id"]))
                elif action == "restore_skill":
                    restore_target_skill(target_conn, int(form_data["skill_id"]))
                elif action == "restore_indicator":
                    restore_target_indicator(target_conn, int(form_data["indicator_id"]))
                redirect_params = {}
                if form_data.get("q", "").strip():
                    redirect_params["q"] = form_data.get("q", "").strip()
                if form_data.get("scope", "").strip() and form_data.get("scope", "").strip() != "all":
                    redirect_params["scope"] = form_data.get("scope", "").strip()
                location = "/catalog-admin/archive"
                if redirect_params:
                    location += "?" + urlencode(redirect_params)
                return redirect_response(start_response, location)
            finally:
                target_conn.close()

        if path == "/catalog-admin/groups" and method == "GET":
            target_conn = open_target_db(target_db_path)
            try:
                groups = list_target_groups(target_conn)
                html = render(
                    "catalog_admin_groups.html",
                    {
                        "title": "Каталог DB",
                        "groups": groups,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                target_conn.close()

        if path == "/catalog-admin/groups" and method == "POST":
            target_conn = open_target_db(target_db_path)
            try:
                form_data = parse_post_data(environ)
                action = form_data.get("action", "")
                if action == "create_group":
                    create_target_group(
                        target_conn,
                        name=form_data.get("name", "").strip() or "Новая группа",
                        sort_order=int(form_data.get("sort_order", "999") or 999),
                        status=form_data.get("status", "active"),
                    )
                elif action == "update_group":
                    update_target_group(
                        target_conn,
                        group_id=int(form_data["group_id"]),
                        name=form_data.get("name", "").strip() or "Группа",
                        sort_order=int(form_data.get("sort_order", "999") or 999),
                        status=form_data.get("status", "active"),
                    )
                elif action == "remove_group":
                    remove_target_group(target_conn, int(form_data["group_id"]))
                return redirect_response(start_response, "/catalog-admin/groups")
            finally:
                target_conn.close()

        if path.startswith("/catalog-admin/groups/"):
            try:
                group_id = int(path.split("/")[-1])
            except ValueError:
                return not_found(start_response)

            target_conn = open_target_db(target_db_path)
            try:
                if method == "POST":
                    form_data = parse_post_data(environ)
                    action = form_data.get("action", "")
                    if action == "update_group":
                        update_target_group(
                            target_conn,
                            group_id=group_id,
                            name=form_data.get("name", "").strip() or "Группа",
                            sort_order=int(form_data.get("sort_order", "999") or 999),
                            status=form_data.get("status", "active"),
                        )
                    elif action == "create_skill":
                        create_target_skill(
                            target_conn,
                            group_id=group_id,
                            name=form_data.get("name", "").strip() or "Новый skill",
                            sort_order=int(form_data.get("sort_order", "999") or 999),
                            description=form_data.get("description", ""),
                            source_skill_name=form_data.get("source_skill_name", ""),
                            resolution_status=form_data.get("resolution_status", "manual"),
                            match_note=form_data.get("match_note", ""),
                            is_active=1 if form_data.get("is_active", "1") == "1" else 0,
                        )
                    elif action == "remove_skill":
                        skill_id = int(form_data.get("skill_id", "0"))
                        if skill_id:
                            remove_target_skill(target_conn, skill_id)
                    elif action == "remove_group":
                        remove_target_group(target_conn, group_id)
                        return redirect_response(start_response, "/catalog-admin/groups")
                    return redirect_response(start_response, f"/catalog-admin/groups/{group_id}")

                group = get_target_group(target_conn, group_id)
                if not group:
                    return not_found(start_response, "Group not found")
                skills = list_target_group_skills(target_conn, group_id)
                html = render(
                    "catalog_admin_group_detail.html",
                    {
                        "title": group["name"],
                        "group": group,
                        "skills": skills,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                target_conn.close()

        if path.startswith("/catalog-admin/skills/"):
            try:
                skill_id = int(path.split("/")[-1])
            except ValueError:
                return not_found(start_response)

            target_conn = open_target_db(target_db_path)
            try:
                if method == "POST":
                    form_data = parse_post_data(environ)
                    action = form_data.get("action", "")
                    if action == "update_skill":
                        update_target_skill(
                            target_conn,
                            skill_id=skill_id,
                            name=form_data.get("name", "").strip() or "Skill",
                            sort_order=int(form_data.get("sort_order", "999") or 999),
                            description=form_data.get("description", ""),
                            source_skill_name=form_data.get("source_skill_name", ""),
                            resolution_status=form_data.get("resolution_status", "manual"),
                            match_note=form_data.get("match_note", ""),
                            is_active=1 if form_data.get("is_active", "1") == "1" else 0,
                        )
                    elif action == "remove_skill":
                        skill = get_target_skill(target_conn, skill_id)
                        group_id = skill["group_id"] if skill else None
                        remove_target_skill(target_conn, skill_id)
                        if group_id is not None:
                            return redirect_response(start_response, f"/catalog-admin/groups/{group_id}")
                        return redirect_response(start_response, "/catalog-admin/groups")
                    elif action == "create_indicator":
                        create_target_indicator(
                            target_conn,
                            skill_id=skill_id,
                            indicator_type=form_data.get("indicator_type", "Не указано"),
                            text=form_data.get("text", "").strip() or "Новый индикатор",
                            sort_order=int(form_data.get("sort_order", "999") or 999),
                            complexity_band=form_data.get("complexity_band", ""),
                            is_active=1 if form_data.get("is_active", "1") == "1" else 0,
                        )
                    elif action == "update_indicator":
                        update_target_indicator(
                            target_conn,
                            indicator_id=int(form_data["indicator_id"]),
                            indicator_type=form_data.get("indicator_type", "Не указано"),
                            text=form_data.get("text", "").strip() or "Индикатор",
                            sort_order=int(form_data.get("sort_order", "999") or 999),
                            complexity_band=form_data.get("complexity_band", ""),
                            is_active=1 if form_data.get("is_active", "1") == "1" else 0,
                        )
                    elif action == "remove_indicator":
                        indicator_id = int(form_data.get("indicator_id", "0"))
                        if indicator_id:
                            remove_target_indicator(target_conn, indicator_id)
                    return redirect_response(start_response, f"/catalog-admin/skills/{skill_id}")

                skill = get_target_skill(target_conn, skill_id)
                if not skill:
                    return not_found(start_response, "Skill not found")
                indicators = list_target_indicators(target_conn, skill_id)
                html = render(
                    "catalog_admin_skill_detail.html",
                    {
                        "title": skill["name"],
                        "skill": skill,
                        "indicators": indicators,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                target_conn.close()

        conn = open_db(db_path)
        try:
            if path == "/reviews/build-dag" and method == "POST":
                ensure_intake_runtime_schema(conn, db_path)
                form_data = parse_post_data(environ)
                try:
                    brief_id = int(form_data.get("brief_id", "0"))
                except ValueError:
                    return not_found(start_response, "Invalid brief id")
                build_result = build_dag_for_brief(conn, brief_id)
                latest_job_id = build_result["state"].get("latest_job_id")
                if latest_job_id:
                    return redirect_response(start_response, f"/intake/jobs/{latest_job_id}")
                return redirect_response(start_response, "/reviews")

            if path == "/reviews" and method == "POST":
                form_data = parse_post_data(environ)
                try:
                    review_id = int(form_data.get("review_id", "0"))
                except ValueError:
                    return not_found(start_response, "Invalid review id")

                new_status = form_data.get("new_status", "open")
                if new_status not in {"open", "resolved", "ignored"}:
                    return not_found(start_response, "Invalid review status")

                update_review_status(conn, review_id, new_status, form_data.get("resolution_note", ""))
                redirect_parts = []
                redirect_status = "open" if new_status in {"resolved", "ignored"} else form_data.get("status", "open")
                if redirect_status:
                    redirect_parts.append(f"status={redirect_status}")
                for key in ("severity", "reason"):
                    value = form_data.get(key, "")
                    if value:
                        redirect_parts.append(f"{key}={value}")
                location = "/reviews"
                if redirect_parts:
                    location += "?" + "&".join(redirect_parts)
                return redirect_response(start_response, location)

            if path == "/competencies":
                query = query_params.get("q", [""])[0].strip()
                scope = query_params.get("scope", ["all"])[0]
                hierarchy_enabled = has_directory_hierarchy(conn)
                if hierarchy_enabled:
                    competencies, directory_profile = list_directory_hierarchy(conn, query, scope)
                else:
                    competencies = list_competencies(conn, query, scope)
                    directory_profile = None
                html = render(
                    "competencies.html",
                    {
                        "title": "Справочник",
                        "query": query,
                        "scope": scope,
                        "competencies": competencies,
                        "directory_profile": directory_profile,
                        "hierarchy_mode": "typed" if hierarchy_enabled else "raw",
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)

            if path.startswith("/competencies/"):
                try:
                    competency_id = int(path.split("/")[-1])
                except ValueError:
                    return not_found(start_response)
                competency = get_competency(conn, competency_id)
                if not competency:
                    return not_found(start_response, "Competency not found")
                skills = get_competency_skills(conn, competency_id)
                html = render(
                    "competency_detail.html",
                    {
                        "title": competency["title"],
                        "competency": competency,
                        "skills": skills,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)

            if path == "/profiles":
                profiles = list_profiles(conn)
                html = render(
                    "profiles.html",
                    {
                        "title": "Скиллсеты",
                        "profiles": profiles,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)

            if path.startswith("/profiles/"):
                try:
                    profile_id = int(path.split("/")[-1])
                except ValueError:
                    return not_found(start_response)
                profile = get_profile(conn, profile_id)
                if not profile:
                    return not_found(start_response, "Profile not found")
                competencies = get_profile_tree(conn, profile_id)
                html = render(
                    "profile_detail.html",
                    {
                        "title": profile["name"],
                        "profile": profile,
                        "competencies": competencies,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)

            if path == "/reviews":
                status_filter = query_params.get("status", ["open"])[0]
                severity_filter = query_params.get("severity", ["all"])[0]
                reason_filter = query_params.get("reason", ["all"])[0]
                status_totals, breakdown, items, reason_codes = list_reviews(
                    conn,
                    status_filter=status_filter,
                    severity_filter=severity_filter,
                    reason_filter=reason_filter,
                )
                html = render(
                    "reviews.html",
                    {
                        "title": "Проверка импорта",
                        "status_totals": status_totals,
                        "breakdown": breakdown,
                        "items": items,
                        "status_filter": status_filter,
                        "severity_filter": severity_filter,
                        "reason_filter": reason_filter,
                        "reason_codes": reason_codes,
                        "dag_build_options": list_dag_build_options(conn),
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)

            if path == "/intake" and method == "GET":
                ensure_intake_runtime_schema(conn, db_path)
                html = render(
                    "intake.html",
                    {
                        "title": "Бриф",
                        "brief": "",
                        "brief_file_path": "",
                        "job": None,
                        "recent_jobs": list_recent_intake_jobs(conn),
                        "result": None,
                        "form_error": None,
                        "upload_name": None,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)

            if path == "/up" and method == "GET":
                ensure_intake_runtime_schema(conn, db_path)
                html = render(
                    "up_index.html",
                    {
                        "title": "Учебные планы",
                        "plans": list_curriculum_plans(conn),
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)

            if path.startswith("/up/plans/"):
                ensure_intake_runtime_schema(conn, db_path)
                segments = [part for part in path.strip("/").split("/") if part]
                if len(segments) >= 3:
                    try:
                        plan_id = int(segments[2])
                    except ValueError:
                        return not_found(start_response, "Invalid curriculum plan id")
                else:
                    return not_found(start_response)

                if len(segments) == 4 and segments[3] == "csv" and method == "GET":
                    plan_payload = get_curriculum_plan(conn, plan_id)
                    if not plan_payload:
                        return not_found(start_response, "Curriculum plan not found")
                    filename = f"curriculum_plan_{plan_id}.csv"
                    return response(
                        start_response,
                        curriculum_plan_to_csv_bytes(plan_payload),
                        content_type="text/csv; charset=utf-8",
                        headers=[("Content-Disposition", f'attachment; filename="{filename}"')],
                    )

                if len(segments) == 5 and segments[3] == "rows" and segments[4] == "new" and method == "POST":
                    plan_payload = get_curriculum_plan(conn, plan_id)
                    if not plan_payload:
                        return not_found(start_response, "Curriculum plan not found")
                    row_id = create_curriculum_plan_row(conn, plan_id)
                    return redirect_response(start_response, f"/up/plans/{plan_id}/rows/{row_id}")

                if len(segments) == 5 and segments[3] == "rows":
                    try:
                        row_id = int(segments[4])
                    except ValueError:
                        return not_found(start_response, "Invalid curriculum plan row id")
                    plan_payload = get_curriculum_plan(conn, plan_id)
                    row_payload = get_curriculum_plan_row(conn, plan_id, row_id)
                    if not plan_payload or not row_payload:
                        return not_found(start_response, "Curriculum plan row not found")
                    if method == "GET":
                        html = render(
                            "up_row_edit.html",
                            {
                                "title": f"Редактирование строки УП #{row_id}",
                                "plan": plan_payload,
                                "row": row_payload,
                                "request_path": "/up",
                            },
                        )
                        return html_response(start_response, html)
                    if method == "POST":
                        form_data = parse_post_data(environ)
                        try:
                            update_curriculum_plan_row(conn, plan_id, row_id, form_data)
                        except ValueError as exc:
                            html = render(
                                "up_row_edit.html",
                                {
                                    "title": f"Редактирование строки УП #{row_id}",
                                    "plan": get_curriculum_plan(conn, plan_id),
                                    "row": {**row_payload, **form_data},
                                    "form_error": str(exc),
                                    "request_path": "/up",
                                },
                            )
                            return html_response(start_response, html, status="400 Bad Request")
                        return redirect_response(start_response, f"/up/plans/{plan_id}")

                if len(segments) == 6 and segments[3] == "rows" and segments[5] == "delete" and method == "POST":
                    try:
                        row_id = int(segments[4])
                    except ValueError:
                        return not_found(start_response, "Invalid curriculum plan row id")
                    delete_curriculum_plan_row(conn, plan_id, row_id)
                    return redirect_response(start_response, f"/up/plans/{plan_id}")

                if len(segments) == 3 and method == "GET":
                    plan_payload = get_curriculum_plan(conn, plan_id)
                    if not plan_payload:
                        return not_found(start_response, "Curriculum plan not found")
                    html = render(
                        "up_detail.html",
                        {
                            "title": f"УП #{plan_id}",
                            "plan": plan_payload,
                            "request_path": path,
                        },
                    )
                    return html_response(start_response, html)

            if path == "/intake" and method == "POST":
                form_data, files = parse_post_form_and_files(environ)
                try:
                    brief_text, upload_name, source_kind, file_path = load_brief_text(form_data, files)
                except ValueError as exc:
                    html = render(
                        "intake.html",
                        {
                            "title": "Бриф",
                            "brief": form_data.get("brief", ""),
                            "brief_file_path": normalize_existing_brief_file_path(form_data.get("brief_file_path", "")),
                            "job": None,
                            "recent_jobs": list_recent_intake_jobs(conn),
                            "result": None,
                            "form_error": str(exc),
                            "upload_name": None,
                            "request_path": path,
                        },
                    )
                    return html_response(start_response, html, status="400 Bad Request")

                if not brief_text:
                    html = render(
                        "intake.html",
                        {
                            "title": "Бриф",
                            "brief": "",
                            "brief_file_path": normalize_existing_brief_file_path(form_data.get("brief_file_path", "")),
                            "job": None,
                            "recent_jobs": list_recent_intake_jobs(conn),
                            "result": None,
                            "form_error": "Нужно вставить текст брифа или загрузить файл.",
                            "upload_name": upload_name,
                            "request_path": path,
                        },
                    )
                    return html_response(start_response, html, status="400 Bad Request")

                from spravochnik_intake.pipeline import config as intake_config

                job_id = create_intake_job(
                    conn,
                    source_kind=source_kind,
                    source_name=upload_name,
                    file_path=file_path,
                    brief_text=brief_text,
                    use_council=intake_config.USE_COUNCIL,
                )
                queue_intake_job(db_path, job_id)
                return redirect_response(start_response, f"/intake/jobs/{job_id}")

            if path.startswith("/intake/jobs/") and path.endswith("/status") and method == "GET":
                try:
                    job_id = int(path.removeprefix("/intake/jobs/").removesuffix("/status"))
                except ValueError:
                    return not_found(start_response)
                ensure_intake_runtime_schema(conn, db_path)
                job = get_intake_job(conn, job_id)
                if not job:
                    return not_found(start_response, "Intake job not found")
                return json_response(
                    start_response,
                    {
                        "id": job["id"],
                        "status": job["status"],
                        "status_label": intake_job_status_label(str(job.get("status"))),
                        "current_stage": job.get("current_stage"),
                        "current_stage_label": intake_stage_label(str(job.get("current_stage"))),
                        "progress_note": job.get("progress_note"),
                        "error_text": job.get("error_text"),
                        "finished_at": job.get("finished_at"),
                    },
                )

            if path.startswith("/intake/jobs/") and path.endswith("/build-dag") and method == "POST":
                try:
                    job_id = int(path.removeprefix("/intake/jobs/").removesuffix("/build-dag"))
                except ValueError:
                    return not_found(start_response)
                ensure_intake_runtime_schema(conn, db_path)
                job = get_intake_job(conn, job_id)
                if not job or not job.get("result_payload"):
                    return not_found(start_response, "Intake job not found")
                brief_id = job["result_payload"].get("brief_id")
                if not isinstance(brief_id, int):
                    return not_found(start_response, "Brief id not found")
                build_result = build_dag_for_brief(conn, brief_id)
                latest_job_id = build_result["state"].get("latest_job_id") or job_id
                return redirect_response(start_response, f"/intake/jobs/{latest_job_id}")

            if path.startswith("/intake/jobs/") and path.endswith("/candidate-decision") and method == "POST":
                try:
                    job_id = int(path.removeprefix("/intake/jobs/").removesuffix("/candidate-decision"))
                except ValueError:
                    return not_found(start_response)
                ensure_intake_runtime_schema(conn, db_path)
                form_data = parse_post_data(environ)
                try:
                    suggestion_id = int(form_data.get("suggestion_id", "0"))
                except ValueError:
                    return not_found(start_response, "Invalid suggestion id")
                action = form_data.get("candidate_action", "")
                if action not in {"accept", "reject", "review"}:
                    return not_found(start_response, "Invalid candidate action")
                target_decision = "needs_review"
                resolution_note = "Возвращено на review из intake-таблицы."
                if action == "accept":
                    target_decision = "accepted"
                    resolution_note = "Подтверждено из intake-таблицы."
                elif action == "reject":
                    target_decision = "rejected"
                    resolution_note = "Отклонено из intake-таблицы."
                apply_candidate_decision(
                    conn,
                    suggestion_id,
                    target_decision,
                    resolution_note,
                )
                return redirect_response(start_response, f"/intake/jobs/{job_id}")

            if path.startswith("/intake/jobs/") and path.endswith("/plan.csv") and method == "GET":
                try:
                    job_id = int(path.removeprefix("/intake/jobs/").removesuffix("/plan.csv"))
                except ValueError:
                    return not_found(start_response)
                ensure_intake_runtime_schema(conn, db_path)
                job = get_intake_job(conn, job_id)
                if not job:
                    return not_found(start_response, "Intake job not found")
                result_payload = job.get("result_payload")
                if not isinstance(result_payload, dict):
                    return not_found(start_response, "Curriculum plan not found")
                plan_payload = result_payload.get("curriculum_plan")
                if not isinstance(plan_payload, dict) or not plan_payload.get("rows"):
                    return not_found(start_response, "Curriculum plan rows not found")
                filename = f"curriculum_plan_brief_{result_payload.get('brief_id', job_id)}.csv"
                return response(
                    start_response,
                    curriculum_plan_to_csv_bytes(plan_payload),
                    content_type="text/csv; charset=utf-8",
                    headers=[("Content-Disposition", f'attachment; filename="{filename}"')],
                )

            if path.startswith("/intake/jobs/") and method == "GET":
                try:
                    job_id = int(path.removeprefix("/intake/jobs/"))
                except ValueError:
                    return not_found(start_response)

                ensure_intake_runtime_schema(conn, db_path)
                job = get_intake_job(conn, job_id)
                if not job:
                    return not_found(start_response, "Intake job not found")

                result = job.get("result_payload") if job.get("status") == "succeeded" else None
                result = hydrate_job_result_payload(conn, result)
                dag_build_state = None
                if isinstance(result, dict) and isinstance(result.get("brief_id"), int):
                    dag_build_state = get_brief_dag_state(conn, int(result["brief_id"]))
                html = render(
                    "intake.html",
                    {
                        "title": f"Бриф #{job_id}",
                        "brief": job.get("brief_text", ""),
                        "brief_file_path": normalize_existing_brief_file_path(job.get("file_path", "")),
                        "job": job,
                        "recent_jobs": list_recent_intake_jobs(conn),
                        "result": result,
                        "dag_build_state": dag_build_state,
                        "form_error": None if job.get("status") != "failed" else f"Ошибка intake-пайплайна: {job.get('error_text')}",
                        "upload_name": job.get("source_name"),
                        "request_path": "/intake",
                    },
                )
                return html_response(start_response, html)

            return not_found(start_response)
        finally:
            conn.close()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local read-only viewer for the imported skills catalog.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to SQLite catalog database.")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY, help="Path to summary JSON.")
    parser.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB, help="Path to target SQLite catalog DB.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8010, help="Port to bind.")
    args = parser.parse_args()

    app = create_app(args.db.resolve(), args.summary.resolve(), args.target_db.resolve())
    with make_server(args.host, args.port, app) as server:
        print(f"Catalog UI listening on http://{args.host}:{args.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
