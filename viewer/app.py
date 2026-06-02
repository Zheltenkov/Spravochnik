from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
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
INTAKE_SCHEMA_SQL = BASE_DIR.parent / "new_tables.sql"
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
TARGET_SCHEMA_READY: set[str] = set()
INTAKE_SCHEMA_READY: set[str] = set()
INTAKE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="intake")

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
    "composite_decomposed": "Кандидат разбит на атомарные части",
    "non_skill:competency_block": "Это блок программы, а не skill",
    "non_skill:curriculum_section": "Это учебный раздел, а не skill",
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
    "resolve": "Резолв против каталога",
    "council": "Экспертное жюри",
    "triage": "Финальный триаж",
    "prerequisites": "Пререквизиты",
    "persist": "Запись в БД",
    "completed": "Завершено",
    "failed": "Ошибка",
}


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
    if resolved in INTAKE_SCHEMA_READY and table_exists(conn, "profile_brief"):
        return
    conn.executescript(INTAKE_SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()
    INTAKE_SCHEMA_READY.add(resolved)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


def load_brief_text(
    form_data: dict[str, str],
    files: dict[str, UploadedFile],
) -> tuple[str, str | None, str, str | None]:
    file_path_raw = form_data.get("brief_file_path", "").strip()
    if file_path_raw:
        brief_text, source_name = load_brief_text_from_path(file_path_raw)
        return brief_text, source_name, "file", file_path_raw

    uploaded_file = files.get("brief_file")
    if uploaded_file:
        suffix = Path(uploaded_file.filename).suffix.casefold()
        brief_text = extract_brief_text_from_bytes(uploaded_file.data, suffix)
        return brief_text, uploaded_file.filename, "file", uploaded_file.filename

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
    from spravochnik_intake.pipeline import stage_brief_to_catalog, stage_catalog_to_dag, storage
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
        raw_candidates = stage_brief_to_catalog.synthesize(evidence, spec)
        notify("atomize", "Проверка атомарности кандидатов, разбиение составных формулировок и реклассификация не-навыков.")
        candidates = stage_brief_to_catalog.atomize_candidates(raw_candidates)
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
        stage_brief_to_catalog.triage_candidates(candidates)
        candidate_metrics = stage_brief_to_catalog.build_candidate_metrics(candidates)
    finally:
        repo.con.close()

    notify("prerequisites", "Формирование графа пререквизитов, снятие циклов и топологический порядок.")
    _edges, dag, removed_cycle, removed_transitive, dag_payload = stage_catalog_to_dag.run(candidates)
    notify("persist", "Запись результатов в каталог и очередь проверки.")
    brief_id = storage.save_brief(conn, brief_text, spec)
    evidence_map = storage.save_evidence(conn, brief_id, evidence)
    storage.save_suggestions(conn, brief_id, candidates, evidence_map)
    prereq_count = storage.save_prerequisites(conn, dag, candidates)
    prereq_review_count = storage.save_prerequisite_reviews(conn, brief_id, dag_payload["edge_review_queue"])
    review_open = conn.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'open'").fetchone()[0]
    by_tid = {candidate.tmp_id: candidate for candidate in candidates}
    atomize_events = []
    for candidate in candidates:
        if candidate.atomicity == "composite":
            atomize_events.append(
                {
                    "parent_name": candidate.name,
                    "verdict": "composite",
                    "children": [child.name for child in candidates if child.parent_tmp_id == candidate.tmp_id],
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

    return {
        "brief_id": brief_id,
        "spec": spec,
        "candidates": [
            {
                "name": candidate.name,
                "group": candidate.group,
                "bloom": candidate.bloom,
                "entity_type": candidate.entity_type,
                "atomicity": candidate.atomicity,
                "parent_tmp_id": candidate.parent_tmp_id,
                "parent_name": by_tid[candidate.parent_tmp_id].name if candidate.parent_tmp_id and candidate.parent_tmp_id in by_tid else None,
                "resolution": candidate.resolution,
                "canonical_name": candidate.canonical_name,
                "confidence": f"{candidate.confidence:.2f}" if candidate.confidence else "—",
                "council_agreement": None if candidate.council_agreement is None else f"{candidate.council_agreement:.2f}",
                "decision": candidate.decision,
                "reasons": ", ".join(review_reason_label(reason) for reason in candidate.reasons) if candidate.reasons else "",
                "tools": ", ".join(candidate.tools) if candidate.tools else "—",
            }
            for candidate in candidates
            if candidate.atomicity in {"atomic", "non_skill"}
        ],
        "atomize": {
            "raw_count": len(raw_candidates),
            "atomic_count": len([candidate for candidate in candidates if candidate.atomicity == "atomic"]),
            "composite_count": len([candidate for candidate in candidates if candidate.atomicity == "composite"]),
            "non_skill_count": len([candidate for candidate in candidates if candidate.atomicity == "non_skill"]),
            "events": atomize_events,
        },
        "dag": dag_payload,
        "persisted": {
            "evidence_source": len(evidence),
            "skill_suggestion": len(candidates),
            "skill_prerequisite": prereq_count,
            "prerequisite_reviews": prereq_review_count,
            "review_open": review_open,
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


def queue_intake_job(db_path: Path, job_id: int) -> None:
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
    conn.commit()


def slugify(value: str) -> str:
    lowered = value.casefold().replace("ё", "е")
    lowered = "-".join(part for part in "".join(ch if ch.isalnum() else "-" for ch in lowered).split("-") if part)
    return lowered or "item"


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
            ],
            "complexity_options": COMPLEXITY_OPTIONS,
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
                for key in ("status", "severity", "reason"):
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
                            "brief_file_path": form_data.get("brief_file_path", ""),
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
                            "brief_file_path": form_data.get("brief_file_path", ""),
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
                html = render(
                    "intake.html",
                    {
                        "title": f"Бриф #{job_id}",
                        "brief": job.get("brief_text", ""),
                        "brief_file_path": job.get("file_path", "") or "",
                        "job": job,
                        "recent_jobs": list_recent_intake_jobs(conn),
                        "result": result,
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
