from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from difflib import SequenceMatcher
import io
import json
from math import isfinite
import mimetypes
import re
import sqlite3
import sys
import unicodedata
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
from viewer.migrations import apply_runtime_migrations, migrate_review_queue_entity_types
from viewer.observability import (
    build_decision_rationale,
    build_intake_quality_metrics,
    build_job_observability,
    load_llm_usage_summary,
)
from viewer.route_zones import detect_route_zone, get_main_nav, get_secondary_nav, show_secondary_nav

DEFAULT_DB = BASE_DIR.parent / "artifacts" / "skills_catalog.sqlite"
DEFAULT_SUMMARY = BASE_DIR.parent / "artifacts" / "catalog_summary.json"
DEFAULT_COMPARE_REPORT = BASE_DIR.parent / "artifacts" / "live_catalog_comparison.json"
INTAKE_SCHEMA_SQL = BASE_DIR.parent / "spravochnik_intake" / "sql" / "new_tables.sql"
CATALOG_ADMIN_SCHEMA_READY: set[str] = set()
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

ARTIFACT_FAMILY_OPTIONS = [
    ("analysis", "Аналитический вывод"),
    ("document", "Комплект документов"),
    ("configuration", "Рабочая настройка"),
    ("design", "Проектное решение"),
    ("production", "Созданный продуктовый результат"),
    ("practice", "Практический результат"),
]
ARTIFACT_SCOPE_TYPE_OPTIONS = [
    ("coverage_area", "Область покрытия"),
    ("skill_group", "Группа навыков"),
    ("taxonomy_node", "Узел таксономии"),
    ("any", "Любая область"),
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
    "low_confidence": "Низкая уверенность",
    "single_source": "Недостаточно подтверждающих источников",
    "council_split": "Модели не согласились между собой",
    "catalog_match_suspicious": "Подозрительный match с каталогом: нужно проверить смысл и группу canonical skill",
    "new_competency_candidate": "Новая competency требует подтверждения",
    "missing_observable_action": "Название не похоже на наблюдаемый навык: нет действия или отглагольного существительного",
    "auto_accept_policy": "Автопринято по policy: уверенность >= 0.95 и согласие жюри = 1.00",
    "composite_decomposed": "Кандидат разбит на атомарные части",
    "non_skill:competency_block": "Это блок программы, а не skill",
    "non_skill:curriculum_section": "Это учебный раздел, а не skill",
    "program_brief_publication_guardrail": "Новый skill из program brief требует методологического подтверждения",
    "needs_review": "Нужна методологическая проверка",
    "cycle_broken": "Цикл в графе был разорван",
    "redundant_transitive": "Ребро признано транзитивно избыточным",
    "bloom_direction": "Возможный спорный порядок по уровню сложности",
    "ai_proposed": "Связь предложена системой и требует проверки",
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
    "draft": "Черновик навыков",
    "atomize": "Атомизация кандидатов",
    "normalize": "Нормализация и дедупликация",
    "resolve": "Сопоставление с каталогом",
    "search": "Поиск evidence по серой зоне",
    "council": "Экспертное жюри",
    "triage": "Финальный триаж",
    "ready_for_review": "Готово к проверке",
    "prerequisites": "Пререквизиты",
    "persist": "Запись в БД",
    "catalog_apply": "Применение в справочник",
    "templates": "Шаблоны УП",
    "plan": "Черновик УП",
    "completed": "Завершено",
    "failed": "Ошибка",
}
INTAKE_PROGRESS_STEPS = [
    {"code": "queued", "label": "Очередь"},
    {"code": "decompose", "label": "Декомпозиция"},
    {"code": "draft", "label": "Черновик"},
    {"code": "normalize", "label": "Нормализация"},
    {"code": "resolve", "label": "Сопоставление"},
    {"code": "search", "label": "Поиск"},
    {"code": "council", "label": "Жюри"},
    {"code": "persist", "label": "Запись"},
    {"code": "ready_for_review", "label": "Проверка"},
    {"code": "completed", "label": "Готово"},
]


def normalize_search_text(value: object | None) -> str:
    if value is None:
        return ""
    return " ".join(str(value).casefold().replace("ё", "е").split())


def normalize_catalog_key(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value).casefold().replace("ё", "е")
    normalized = "".join(char if char.isalnum() or char in {"+", " "} else " " for char in text)
    return " ".join(normalized.split())


def review_reason_label(reason_code: str | None) -> str:
    if not reason_code:
        return "Нужна проверка"
    if "," in reason_code:
        labels = [
            REVIEW_REASON_LABELS.get(part.strip(), part.strip().replace("_", " "))
            for part in reason_code.split(",")
            if part.strip()
        ]
        return "; ".join(labels) if labels else "Нужна проверка"
    return REVIEW_REASON_LABELS.get(reason_code, reason_code.replace("_", " "))


def edge_reason_label(value: object | None) -> str:
    """Translate one or several stored edge reason codes into UI labels."""
    if value is None:
        return "—"
    if isinstance(value, str):
        raw_items = value.split(",")
    else:
        raw_items = [str(item) for item in value if item]
    labels = [review_reason_label(item.strip()) for item in raw_items if item and item.strip()]
    return ", ".join(labels) if labels else "—"


def review_severity_label(severity: str | None) -> str:
    if not severity:
        return "—"
    return REVIEW_SEVERITY_LABELS.get(severity, severity.replace("_", " "))


def review_status_label(status: str | None) -> str:
    if not status:
        return "—"
    return REVIEW_STATUS_LABELS.get(status, status.replace("_", " "))


REVIEW_ENTITY_LABELS = {
    "skill": "Навык",
    "competency": "Компетенция",
    "indicator_row": "Индикатор",
    "profile": "Профиль",
    "project": "Проект",
    "project_indicator": "Индикатор проекта",
    "prerequisite_edge": "Связь зависимостей",
    "ai_analysis_run": "Запуск анализа",
    "workbook": "Файл",
    "sheet": "Лист",
    "block": "Блок",
}


REVIEW_TEXT_REPLACEMENTS = {
    "program_brief_publication_guardrail": "новый навык из брифа требует методологического подтверждения",
    "catalog_match_suspicious": "подозрительное совпадение с каталогом",
    "missing_observable_action": "нет наблюдаемого действия",
    "fuzzy_match_ambiguous": "неоднозначное похожее совпадение",
    "auto_accept_policy": "автопринято по правилу",
    "novel_skill": "новый навык",
    "low_confidence": "низкая уверенность",
    "single_source": "недостаточно источников",
    "council_split": "жюри не согласилось",
    "needs_review": "нужно проверить",
    "bloom_direction": "возможный спорный порядок по уровню сложности",
    "ai_proposed": "связь предложена системой",
    "prerequisite_edge": "связь зависимостей",
    "edge_key": "код связи",
    "src_id": "исходный навык",
    "dst_id": "следующий навык",
    "edge_label": "связь",
    "confidence": "уверенность",
    "source": "источник",
    "relation_type": "тип связи",
    "soft": "мягкая методическая связь",
    "Резолв против каталога": "Сопоставление с каталогом",
    "Атомарность": "Атомарность",
    "competency": "компетенция",
    "skills": "навыки",
    "atomic": "атомарный",
    "new": "новый",
    "matched": "найдено совпадение",
    "alias": "найден синоним",
    "fuzzy": "похожий вариант",
    "skill": "навык",
}


def review_entity_label(entity_type: str | None) -> str:
    if not entity_type:
        return "Объект"
    return REVIEW_ENTITY_LABELS.get(entity_type, entity_type.replace("_", " "))


def review_source_label(source_ref: str | None) -> str:
    if not source_ref:
        return "Источник не указан"
    source = str(source_ref)
    if source.startswith("brief:"):
        return f"Бриф #{source.split(':', 1)[1]}"
    if source.startswith("intake_accept:"):
        return f"Принятие в справочник #{source.split(':', 1)[1]}"
    return source.replace("_", " ")


def review_text_label(text: object | None) -> str:
    if text is None:
        return "—"
    normalized = str(text)
    for source, replacement in sorted(REVIEW_TEXT_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        normalized = normalized.replace(source, replacement)
    return normalized


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
    resolved = str(Path(db_path).resolve())
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_function("search_norm", 1, normalize_search_text)
    ensure_runtime_schema(conn)
    if resolved not in CATALOG_ADMIN_SCHEMA_READY:
        ensure_catalog_admin_runtime_schema(conn)
        CATALOG_ADMIN_SCHEMA_READY.add(resolved)
    return conn


def load_summary(summary_path: Path) -> dict[str, object]:
    if not summary_path.exists():
        return {}
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def refresh_summary_counts(summary: dict[str, object], db_path: Path) -> dict[str, object]:
    refreshed = dict(summary or {})
    counts = dict(refreshed.get("counts") or {})
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        counts.update(
            {
                "profiles": int(conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0]) if table_exists(conn, "profile") else counts.get("profiles", 0),
                "competencies": (
                    int(conn.execute("SELECT COUNT(*) FROM competency").fetchone()[0])
                    if table_exists(conn, "competency")
                    else (
                        int(conn.execute("SELECT COUNT(*) FROM profile_competency").fetchone()[0])
                        if table_exists(conn, "profile_competency")
                        else counts.get("competencies", 0)
                    )
                ),
                "skills": int(conn.execute("SELECT COUNT(*) FROM skill WHERE status = 'active'").fetchone()[0]) if table_exists(conn, "skill") else counts.get("skills", 0),
                "indicator_rows": int(conn.execute("SELECT COUNT(*) FROM indicator_row").fetchone()[0]) if table_exists(conn, "indicator_row") else counts.get("indicator_rows", 0),
                "open_reviews": int(conn.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'open'").fetchone()[0]) if table_exists(conn, "review_queue") else counts.get("open_reviews", 0),
            }
        )
        conn.close()
    except sqlite3.Error:
        pass
    refreshed["counts"] = counts
    return refreshed


def repair_dirty_profile_names(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "profile"):
        return 0
    updated = 0
    for row in conn.execute("SELECT id, name, slug FROM profile ORDER BY id").fetchall():
        current_name = str(row["name"] or "")
        cleaned_name = clean_profile_name(current_name)
        current_slug = str(row["slug"] or "")
        cleaned_slug = clean_profile_slug(current_slug)
        if cleaned_name and cleaned_name != current_name:
            conn.execute("UPDATE profile SET name = ? WHERE id = ?", (cleaned_name, row["id"]))
            updated += 1
        if cleaned_slug and cleaned_slug != current_slug:
            exists = conn.execute(
                "SELECT 1 FROM profile WHERE slug = ? AND id != ?",
                (cleaned_slug, row["id"]),
            ).fetchone()
            if not exists:
                conn.execute("UPDATE profile SET slug = ? WHERE id = ?", (cleaned_slug, row["id"]))
                updated += 1
    if updated:
        conn.commit()
    return updated


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


def parse_path_int(path: str, prefix: str, suffix: str = "") -> int | None:
    if not path.startswith(prefix) or (suffix and not path.endswith(suffix)):
        return None
    end = -len(suffix) if suffix else None
    try:
        return int(path[len(prefix) : end])
    except ValueError:
        return None


def clean_profile_name(value: str | None) -> str:
    cleaned = str(value or "").strip()
    cleaned = re.sub(r"[_\s-]*warning$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"_{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or str(value or "").strip()


def clean_profile_slug(value: str | None) -> str:
    cleaned = str(value or "").strip()
    cleaned = re.sub(r"[_-]*warning$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"_{2,}", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or str(value or "").strip()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(row["name"] == column_name for row in conn.execute(f"PRAGMA table_info({table_name})"))


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


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


def format_catalog_similarity(score: float | int | None) -> tuple[str | None, str | None]:
    """Return UI-ready catalog similarity and novelty scores on a 0..100 scale."""
    if score is None:
        return None, None
    bounded_score = max(0.0, min(100.0, float(score)))
    return f"{bounded_score:.2f}", f"{100.0 - bounded_score:.2f}"


def _reason_set(reasons: list[str] | tuple[str, ...] | str | None) -> set[str]:
    if reasons is None:
        return set()
    if isinstance(reasons, str):
        parts = {part.strip() for part in re.split(r"[,;]\s*", reasons) if part.strip()}
        lowered = reasons.casefold()
        if "подозрительный match" in lowered or "catalog_match_suspicious" in lowered:
            parts.add("catalog_match_suspicious")
        return parts
    return {str(reason).strip() for reason in reasons if str(reason).strip()}


def build_similarity_hint(
    score: float | int | None,
    resolution: str | None,
    has_nearest: bool,
    reasons: list[str] | tuple[str, ...] | str | None = None,
) -> dict[str, str]:
    """Explain how a catalog similarity score should be interpreted."""
    reason_set = _reason_set(reasons)
    if "catalog_match_suspicious" in reason_set:
        return {
            "label": "Подозрительный матч",
            "class": "weak",
            "recommendation": "Не используйте canonical skill автоматически. Нужно проверить смысл, группу и индикаторы.",
        }
    try:
        bounded_score = None if score is None else max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        bounded_score = None
    if bounded_score is None:
        return {
            "label": "Нет данных",
            "class": "neutral",
            "recommendation": "Нет ближайшего совпадения для методологической сверки.",
        }
    normalized_resolution = str(resolution or "").casefold()
    if normalized_resolution in {"matched", "alias"}:
        return {
            "label": "Покрывает",
            "class": "strong",
            "recommendation": "Кандидат уже покрыт существующим skill. Используйте canonical skill в DAG.",
        }
    if normalized_resolution == "fuzzy" or bounded_score >= 90.0:
        return {
            "label": "Почти эквивалент",
            "class": "strong",
            "recommendation": "Лучше привязать к существующему skill, если индикаторы покрывают смысл брифа.",
        }
    if has_nearest and bounded_score >= 75.0:
        return {
            "label": "Частично похоже",
            "class": "medium",
            "recommendation": "Проверьте индикаторы ближайшего skill: если они покрывают требование, используйте привязку.",
        }
    if has_nearest:
        return {
            "label": "Слабое совпадение",
            "class": "weak",
            "recommendation": "Не привязывайте автоматически. Обычно это новый skill или кандидат на отклонение.",
        }
    return {
        "label": "Новое",
        "class": "neutral",
        "recommendation": "Похожего skill не найдено. Решение: добавить новый или отклонить как нерелевантный.",
    }


def build_candidate_recommended_action(
    score: float | int | None,
    resolution: str | None,
    has_nearest: bool,
    nearest_name: str | None = None,
    reasons: list[str] | tuple[str, ...] | str | None = None,
    decision: str | None = None,
) -> dict[str, str]:
    """Return deterministic methodologist action for a resolved candidate."""
    normalized_decision = str(decision or "").casefold()
    normalized_resolution = str(resolution or "").casefold()
    reason_set = _reason_set(reasons)
    target = str(nearest_name or "").strip()
    try:
        bounded_score = None if score is None else max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        bounded_score = None

    if normalized_decision == "accepted":
        return {
            "code": "done",
            "label": "Уже принято",
            "target": target,
            "detail": "Кандидат используется в каталоге/DAG.",
        }
    if normalized_decision == "rejected":
        return {
            "code": "rejected",
            "label": "Отклонено",
            "target": "",
            "detail": "Кандидат не используется для покрытия брифа.",
        }
    if "catalog_match_suspicious" in reason_set:
        return {
            "code": "check",
            "label": "Проверить match",
            "target": target,
            "detail": "Есть риск ложного совпадения: группа, смысл или coverage area конфликтуют.",
        }
    if has_nearest and normalized_resolution in {"matched", "alias", "fuzzy"}:
        return {
            "code": "link",
            "label": "Покрыть существующим",
            "target": target,
            "detail": "Проверьте индикаторы nearest skill и привяжите, если смысл закрыт.",
        }
    if has_nearest and bounded_score is not None and bounded_score >= 75.0:
        return {
            "code": "link",
            "label": "Вероятно покрыть существующим",
            "target": target,
            "detail": "Похожесть высокая: сначала проверьте ближайший skill, потом решайте про новый.",
        }
    if normalized_resolution == "new" or not has_nearest:
        return {
            "code": "create",
            "label": "Создать новый skill",
            "target": "",
            "detail": "Похожего покрытия нет или оно слишком слабое.",
        }
    return {
        "code": "review",
        "label": "Оставить на review",
        "target": target,
        "detail": "Недостаточно данных для безопасного автодействия.",
    }


def load_nearest_skill_preview(conn: sqlite3.Connection, skill_id: int | None, indicator_limit: int = 3) -> dict[str, object] | None:
    """Load a compact catalog preview for the nearest matched skill."""
    if not skill_id or not table_exists(conn, "skill"):
        return None
    skill_cols = table_columns(conn, "skill")
    if "name" in skill_cols and "canonical_name" in skill_cols:
        name_expr = "COALESCE(s.name, s.canonical_name)"
    elif "canonical_name" in skill_cols:
        name_expr = "s.canonical_name"
    elif "name" in skill_cols:
        name_expr = "s.name"
    else:
        name_expr = "s.normalized_name"
    canonical_expr = "s.canonical_name" if "canonical_name" in skill_cols else name_expr
    has_skill_group = table_exists(conn, "skill_group") and "group_id" in skill_cols
    if has_skill_group:
        row = conn.execute(
            f"""
            SELECT s.id, {name_expr} AS name, {canonical_expr} AS canonical_name, sg.name AS group_name
            FROM skill s
            LEFT JOIN skill_group sg ON sg.id = s.group_id
            WHERE s.id = ?
            """,
            (skill_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT s.id, {name_expr} AS name, {canonical_expr} AS canonical_name, NULL AS group_name
            FROM skill s
            WHERE s.id = ?
            """,
            (skill_id,),
        ).fetchone()
    if not row:
        return None
    preview = {
        "id": int(row["id"]),
        "name": row["canonical_name"] or row["name"],
        "group": row["group_name"],
        "indicators": [],
    }
    if table_exists(conn, "indicator"):
        indicator_cols = table_columns(conn, "indicator")
        text_col = "text" if "text" in indicator_cols else None
        if text_col:
            select_cols = ["id", text_col]
            if "indicator_type" in indicator_cols:
                select_cols.append("indicator_type")
            if "complexity_label" in indicator_cols:
                select_cols.append("complexity_label")
            if "complexity_band" in indicator_cols:
                select_cols.append("complexity_band")
            order_sql = "sort_order, id" if "sort_order" in indicator_cols else "id"
            active_filter = "AND COALESCE(is_active, 1) = 1" if "is_active" in indicator_cols else ""
            rows = conn.execute(
                f"""
                SELECT {', '.join(select_cols)}
                FROM indicator
                WHERE skill_id = ?
                {active_filter}
                ORDER BY {order_sql}
                LIMIT ?
                """,
                (skill_id, indicator_limit),
            ).fetchall()
            preview["indicators"] = [
                {
                    "text": str(indicator[text_col] or ""),
                    "type": str(indicator["indicator_type"] or "") if "indicator_type" in indicator.keys() else "",
                    "complexity": (
                        str(indicator["complexity_label"] or "")
                        if "complexity_label" in indicator.keys()
                        else str(indicator["complexity_band"] or "") if "complexity_band" in indicator.keys() else ""
                    ),
                }
                for indicator in rows
                if str(indicator[text_col] or "").strip()
            ]
    return preview


def ensure_intake_runtime_schema(conn: sqlite3.Connection, db_path: Path) -> None:
    resolved = str(db_path.resolve())
    schema_ready = (
        table_exists(conn, "profile_brief")
        and table_exists(conn, "curriculum_plan")
        and table_exists(conn, "curriculum_plan_row")
        and table_exists(conn, "curriculum_artifact_template")
        and table_exists(conn, "curriculum_artifact_template_scope")
        and table_exists(conn, "curriculum_artifact_template_proposal")
        and table_exists(conn, "skill_set")
        and table_exists(conn, "skill_set_item")
        and table_exists(conn, "prerequisite_edge_decision")
        and column_exists(conn, "skill_suggestion", "coverage_area")
        and column_exists(conn, "skill_suggestion", "source_name")
        and column_exists(conn, "skill_suggestion", "indicators_json")
        and column_exists(conn, "skill_suggestion", "match_score")
        and column_exists(conn, "skill_suggestion", "nearest_skill_id")
        and column_exists(conn, "skill_suggestion", "nearest_name")
        and column_exists(conn, "curriculum_plan_row", "weighted_skills")
        and column_exists(conn, "curriculum_plan_row", "completion_percent")
        and column_exists(conn, "curriculum_plan_row", "validation_criteria")
    )
    if resolved not in INTAKE_SCHEMA_READY or not schema_ready:
        apply_runtime_migrations(conn, INTAKE_SCHEMA_SQL)
        repair_intake_review_links(conn)
        INTAKE_SCHEMA_READY.add(resolved)
    migrate_review_queue_entity_types(conn)
    repair_stale_intake_jobs(conn)


def prune_empty_generated_catalog_nodes(conn: sqlite3.Connection) -> dict[str, int]:
    """Remove empty generated taxonomy nodes; keep manual empty nodes editable."""
    stats: dict[str, int] = {}
    if table_exists(conn, "skill_set") and table_exists(conn, "skill_set_item"):
        stats["skill_set_orphan_items"] = conn.execute(
            """
            DELETE FROM skill_set_item
            WHERE skill_set_id NOT IN (SELECT id FROM skill_set)
               OR skill_id NOT IN (SELECT id FROM skill)
            """
        ).rowcount
        stats["skill_set_empty_archived"] = conn.execute(
            """
            UPDATE skill_set
            SET status = 'archived',
                updated_at = ?
            WHERE status != 'archived'
              AND NOT EXISTS (
                  SELECT 1
                  FROM skill_set_item ssi
                  WHERE ssi.skill_set_id = skill_set.id
              )
            """,
            (datetime.now(UTC).isoformat(),),
        ).rowcount

    if table_exists(conn, "skill_group") and table_exists(conn, "skill"):
        stats["skill_group_empty_generated_archived"] = conn.execute(
            """
            UPDATE skill_group
            SET status = 'deprecated',
                updated_at = ?
            WHERE COALESCE(source, '') IN ('derived', 'live_snapshot', 'intake_accept')
              AND status != 'deprecated'
              AND NOT EXISTS (
                  SELECT 1
                  FROM skill s_active
                  WHERE s_active.group_id = skill_group.id
                    AND COALESCE(s_active.is_active, 1) = 1
              )
              AND EXISTS (
                  SELECT 1
                  FROM skill s_any
                  WHERE s_any.group_id = skill_group.id
              )
            """,
            (datetime.now(UTC).isoformat(),),
        ).rowcount
        stats["skill_group_empty_generated_deleted"] = conn.execute(
            """
            DELETE FROM skill_group
            WHERE COALESCE(source, '') IN ('derived', 'live_snapshot', 'intake_accept')
              AND NOT EXISTS (
                  SELECT 1
                  FROM skill s
                  WHERE s.group_id = skill_group.id
              )
            """
        ).rowcount

    if table_exists(conn, "competency") and table_exists(conn, "profile_competency") and table_exists(conn, "competency_skill"):
        stats["profile_competency_empty_deleted"] = conn.execute(
            """
            DELETE FROM profile_competency
            WHERE NOT EXISTS (
                SELECT 1
                FROM competency_skill cs
                WHERE cs.profile_competency_id = profile_competency.id
            )
            AND (
                title_in_source IS NULL
                OR title_in_source = ''
                OR title_in_source = (
                    SELECT title FROM competency c WHERE c.id = profile_competency.competency_id
                )
            )
            """
        ).rowcount
    conn.commit()
    return stats


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso_datetime(value: object | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def format_local_datetime(value: object | None) -> str:
    parsed = parse_iso_datetime(value)
    if not parsed:
        return str(value or "")
    return parsed.strftime("%d.%m.%Y %H:%M")


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


def get_intake_job_brief_id(conn: sqlite3.Connection, job_id: int) -> tuple[dict[str, object] | None, int | None]:
    job = get_intake_job(conn, job_id)
    payload = job.get("result_payload") if job else None
    brief_id = payload.get("brief_id") if isinstance(payload, dict) else None
    return job, brief_id if isinstance(brief_id, int) else None


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
        """
        SELECT COUNT(*)
        FROM review_queue
        WHERE source_ref = ?
          AND entity_type = 'skill'
          AND status = 'open'
          AND NOT (
              json_valid(details)
              AND json_extract(details, '$.review_kind') = 'prerequisite_edge'
          )
        """,
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


def load_prerequisite_edge_decisions(conn: sqlite3.Connection, brief_id: int) -> dict[str, str]:
    if not table_exists(conn, "prerequisite_edge_decision"):
        return {}
    return {
        str(row["edge_key"]): str(row["decision"])
        for row in conn.execute(
            """
            SELECT edge_key, decision
            FROM prerequisite_edge_decision
            WHERE brief_id = ?
            """,
            (brief_id,),
        )
    }


def parse_review_details_json(details: str | None) -> dict[str, object]:
    if not details:
        return {}
    try:
        data = json.loads(details)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def split_review_reason_codes(reason_code: str | None, details: dict[str, object] | None = None) -> list[str]:
    codes: list[str] = []
    raw_reasons = (details or {}).get("reasons")
    if isinstance(raw_reasons, list):
        codes.extend(str(item).strip() for item in raw_reasons if str(item).strip())
    if reason_code:
        codes.extend(part.strip() for part in str(reason_code).split(",") if part.strip())
    seen: set[str] = set()
    unique_codes: list[str] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique_codes.append(code)
    return unique_codes


def format_percent(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 1:
        number *= 100
    return f"{number:.0f}%"


def split_edge_label(edge_label: object | None) -> tuple[str, str]:
    text = str(edge_label or "").strip()
    if " -> " in text:
        src, dst = text.split(" -> ", 1)
        return src.strip(), dst.strip()
    if "→" in text:
        src, dst = text.split("→", 1)
        return src.strip(), dst.strip()
    return text or "первый навык", "следующий навык"


def format_prerequisite_edge_review(item: dict[str, object]) -> None:
    details = parse_review_details_json(str(item.get("details") or ""))
    if details.get("review_kind") != "prerequisite_edge":
        item["display_reason"] = review_text_label(item.get("reason_label"))
        item["display_check"] = review_text_label(item.get("details"))
        return

    src_name, dst_name = split_edge_label(details.get("edge_label"))
    reason_codes = split_review_reason_codes(str(item.get("reason_code") or ""), details)
    reason_labels = [review_reason_label(code) for code in reason_codes]
    confidence = format_percent(details.get("confidence"))
    relation_type = str(details.get("relation_type") or "").strip()

    item["display_reason"] = "; ".join(reason_labels) if reason_labels else "Связь требует методологической проверки"

    notes: list[str] = [
        f"Проверяемая связь: «{src_name}» должен быть изучен до «{dst_name}».",
    ]
    if confidence:
        notes.append(f"Уверенность системы: {confidence}.")
    if relation_type == "soft":
        notes.append("Тип связи: мягкая методическая связь. Она не считается обязательной, пока методолог её не подтвердит.")
    if "bloom_direction" in reason_codes:
        notes.append("Причина проверки: возможен спорный порядок по уровню сложности. Проверьте, не должен ли второй навык идти раньше первого.")
    if "ai_proposed" in reason_codes:
        notes.append("Причина проверки: связь предложена автоматически, поэтому её нельзя использовать в рабочем графе без подтверждения.")
    notes.append("Что решить: подтвердить связь, если первый навык действительно нужен как основа для второго; отклонить, если порядок неверный или связь только тематическая.")
    item["display_check"] = "\n".join(notes)


def save_prerequisite_edge_decision(
    conn: sqlite3.Connection,
    *,
    brief_id: int,
    details: dict[str, object],
    decision: str,
    resolution_note: str,
) -> None:
    if not table_exists(conn, "prerequisite_edge_decision"):
        return
    edge_key = str(details.get("edge_key") or "").strip()
    if "->" not in edge_key:
        return
    src_raw, dst_raw = edge_key.split("->", 1)

    def suggestion_id(raw: object) -> int | None:
        value = str(raw or "").strip()
        if value.startswith("S") and value[1:].isdigit():
            return int(value[1:])
        if value.isdigit():
            return int(value)
        return None

    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO prerequisite_edge_decision(
            brief_id, edge_key, src_suggestion_id, dst_suggestion_id,
            relation_type, confidence, source, decision, resolution_note, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(brief_id, edge_key) DO UPDATE SET
            src_suggestion_id = excluded.src_suggestion_id,
            dst_suggestion_id = excluded.dst_suggestion_id,
            relation_type = excluded.relation_type,
            confidence = excluded.confidence,
            source = excluded.source,
            decision = excluded.decision,
            resolution_note = excluded.resolution_note,
            updated_at = excluded.updated_at
        """,
        (
            brief_id,
            edge_key,
            suggestion_id(details.get("src_id") or src_raw),
            suggestion_id(details.get("dst_id") or dst_raw),
            str(details.get("relation_type") or "soft"),
            parse_optional_float(str(details.get("confidence"))) if details.get("confidence") is not None else None,
            str(details.get("source") or "review"),
            decision,
            resolution_note.strip() or None,
            now,
        ),
    )


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
        "report": {"coverage_ok": False, "order_violations": [], "project_violations": []},
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


def count_brief_template_proposals(conn: sqlite3.Connection, brief_id: int) -> dict[str, int]:
    if not table_exists(conn, "curriculum_artifact_template_proposal"):
        return {"total": 0, "open": 0, "accepted": 0, "rejected": 0}
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_count,
            SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) AS accepted_count,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count
        FROM curriculum_artifact_template_proposal
        WHERE brief_id = ?
        """,
        (brief_id,),
    ).fetchone()
    if not row:
        return {"total": 0, "open": 0, "accepted": 0, "rejected": 0}
    return {
        "total": int(row["total_count"] or 0),
        "open": int(row["open_count"] or 0),
        "accepted": int(row["accepted_count"] or 0),
        "rejected": int(row["rejected_count"] or 0),
    }


def get_brief_catalog_apply_state(conn: sqlite3.Connection, brief_id: int) -> dict[str, int | bool]:
    accepted_atomic = 0
    active_promotions = 0
    active_promoted_skills = 0
    if table_exists(conn, "skill_suggestion"):
        accepted_atomic = int(
            conn.execute(
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
        )
    if table_exists(conn, "skill_promotion_log") and table_exists(conn, "skill_suggestion"):
        promotion_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_promotions,
                    COUNT(DISTINCT spl.skill_id) AS distinct_skills
                FROM skill_promotion_log spl
                JOIN skill_suggestion ss ON ss.id = spl.suggestion_id
                WHERE ss.brief_id = ?
                  AND spl.status = 'active'
                """,
                (brief_id,),
            ).fetchone()
        if isinstance(promotion_row, sqlite3.Row):
            total_promotions = promotion_row["total_promotions"]
            distinct_skills = promotion_row["distinct_skills"]
        else:
            total_promotions = promotion_row[0]
            distinct_skills = promotion_row[1]
        active_promotions = int(total_promotions or 0)
        active_promoted_skills = int(distinct_skills or 0)
    skill_set_items = 0
    if table_exists(conn, "skill_set") and table_exists(conn, "skill_set_item"):
        skill_set_items = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM skill_set_item ssi
                JOIN skill_set ss ON ss.id = ssi.skill_set_id
                WHERE ss.source_type = 'brief'
                  AND ss.source_id = ?
                  AND ss.status = 'active'
                """,
                (brief_id,),
            ).fetchone()[0]
        )
    templates = count_brief_template_proposals(conn, brief_id)
    # Several accepted candidates can legitimately resolve into one canonical skill.
    # DAG/UP readiness must compare skillset rows with unique promoted skills, not
    # with the raw candidate count, otherwise deduplication blocks the workflow.
    catalog_applied = bool(
        accepted_atomic
        and active_promotions >= accepted_atomic
        and skill_set_items >= active_promoted_skills
    )
    return {
        "accepted_atomic": accepted_atomic,
        "active_promotions": active_promotions,
        "active_promoted_skills": active_promoted_skills,
        "skill_set_items": skill_set_items,
        "template_proposals": templates["total"],
        "open_template_proposals": templates["open"],
        "accepted_template_proposals": templates["accepted"],
        "catalog_applied": catalog_applied,
    }


def count_open_skill_reviews_for_brief(conn: sqlite3.Connection, brief_id: int) -> int:
    if not table_exists(conn, "review_queue"):
        return 0
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM review_queue
            WHERE source_ref = ?
              AND entity_type = 'skill'
              AND status = 'open'
              AND NOT (
                  json_valid(details)
                  AND json_extract(details, '$.review_kind') = 'prerequisite_edge'
              )
            """,
            (f"brief:{brief_id}",),
        ).fetchone()[0]
    )


def count_open_prerequisite_edge_reviews_for_brief(conn: sqlite3.Connection, brief_id: int) -> int:
    if not table_exists(conn, "review_queue"):
        return 0
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM review_queue
            WHERE source_ref = ?
              AND entity_type = 'prerequisite_edge'
              AND status = 'open'
              AND json_valid(details)
              AND json_extract(details, '$.review_kind') = 'prerequisite_edge'
            """,
            (f"brief:{brief_id}",),
        ).fetchone()[0]
    )


def load_brief_catalog_promotion_summary(conn: sqlite3.Connection, brief_id: int, limit: int = 10) -> dict[str, object]:
    if not all(table_exists(conn, name) for name in ("skill_promotion_log", "skill_suggestion", "skill")):
        return {"total": 0, "items": []}
    rows = fetch_all(
        conn,
        """
        SELECT
            spl.skill_id,
            spl.suggestion_id,
            spl.status,
            ss.suggested_name,
            ss.resolution,
            s.canonical_name,
            COALESCE(sg.name, ss.group_name, '') AS group_name
        FROM skill_promotion_log spl
        JOIN skill_suggestion ss ON ss.id = spl.suggestion_id
        JOIN skill s ON s.id = spl.skill_id
        LEFT JOIN skill_group sg ON sg.id = s.group_id
        WHERE ss.brief_id = ?
          AND spl.status = 'active'
        ORDER BY spl.id DESC
        LIMIT ?
        """,
        (brief_id, limit),
    )
    total = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM skill_promotion_log spl
            JOIN skill_suggestion ss ON ss.id = spl.suggestion_id
            WHERE ss.brief_id = ?
              AND spl.status = 'active'
            """,
            (brief_id,),
        ).fetchone()[0]
    )
    return {"total": total, "items": rows}


def update_jobs_catalog_payload(
    conn: sqlite3.Connection,
    brief_id: int,
    *,
    catalog_state: dict[str, object],
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
        payload["catalog_state"] = catalog_state
        if persisted_update and isinstance(payload.get("persisted"), dict):
            payload["persisted"].update(persisted_update)
        conn.execute(
            "UPDATE intake_job SET result_payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), utc_now_iso(), row["id"]),
        )
    conn.commit()


def apply_brief_catalog_decisions(conn: sqlite3.Connection, brief_id: int) -> dict[str, object]:
    """Apply accepted skill decisions to the canonical catalog as a batch step."""
    from spravochnik_intake.pipeline import llm as intake_llm
    from spravochnik_intake.pipeline import storage as intake_storage

    clear_brief_dag_artifacts(conn, brief_id)
    clear_brief_curriculum_plan_artifacts(conn, brief_id)
    promotion_stats = intake_storage.sync_promotions_for_brief(conn, brief_id)
    skill_set = intake_storage.sync_brief_skill_set(conn, brief_id)
    plan_payload = build_deferred_curriculum_plan_payload(
        "УП ещё не строился: примите нужные шаблоны УП и запустите построение DAG/УП."
    )
    save_meta = intake_storage.save_curriculum_plan(conn, brief_id, plan_payload)
    plan_payload["plan_id"] = save_meta["plan_id"]
    plan_payload["row_count"] = save_meta["row_count"]
    try:
        intake_llm.set_usage_context(brief_id=brief_id, stage="up_template_consilium")
        template_proposals = intake_storage.generate_curriculum_artifact_template_proposals(
            conn,
            brief_id=brief_id,
            plan_id=int(save_meta["plan_id"]),
        )
    finally:
        intake_llm.clear_usage_context()

    catalog_state = get_brief_catalog_apply_state(conn, brief_id)
    catalog_state.update(
        {
            "last_apply_promoted": int(promotion_stats.get("promoted", 0) or 0),
            "last_apply_reverted": int(promotion_stats.get("reverted", 0) or 0),
            "skill_set_status": skill_set.get("status"),
            "skill_set_id": skill_set.get("skill_set_id"),
        }
    )
    update_jobs_catalog_payload(
        conn,
        brief_id,
        catalog_state=catalog_state,
        persisted_update={
            "catalog_promoted": int(catalog_state.get("active_promotions") or 0),
            "catalog_reverted": int(promotion_stats.get("reverted", 0) or 0),
            "template_proposals": len(template_proposals),
            "skill_set_items": int(catalog_state.get("skill_set_items") or 0),
        },
    )
    state = refresh_brief_dag_state(
        conn,
        brief_id,
        status="catalog_applied",
        message="Справочник и набор навыков обновлены. Теперь можно принять шаблоны УП и построить DAG/УП.",
    )
    plan_payload["template_proposal_count"] = len(template_proposals)
    plan_payload["template_proposal_status"] = "open" if template_proposals else "none"
    conn.execute(
        "UPDATE curriculum_plan SET payload_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (json.dumps(plan_payload, ensure_ascii=False), int(save_meta["plan_id"])),
    )
    conn.commit()
    update_jobs_curriculum_plan_payload(
        conn,
        brief_id,
        plan_payload,
        persisted_update={"curriculum_plan_rows": 0},
    )
    return {
        "brief_id": brief_id,
        "catalog_state": catalog_state,
        "dag_state": state,
        "template_proposals": len(template_proposals),
        "promotion_stats": promotion_stats,
        "skill_set": skill_set,
    }


def hydrate_job_result_payload(conn: sqlite3.Connection, result: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return result
    brief_id = result.get("brief_id")
    if not isinstance(brief_id, int) or not isinstance(result.get("candidates"), list):
        return result
    from spravochnik_intake.pipeline import config as intake_config

    suggestion_rows = conn.execute(
        """
        SELECT id, suggested_name, source_name, group_name, entity_type, atomicity, decision,
               confidence, council_agreement, resolution, match_score,
               nearest_skill_id, nearest_name, nearest_group
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
        match_score_value = float(row["match_score"]) if row["match_score"] is not None else None
        candidate["match_score"], candidate["novelty_score"] = format_catalog_similarity(match_score_value)
        candidate["resolution"] = row["resolution"] or candidate.get("resolution")
        candidate["source_name"] = row["source_name"] or candidate.get("source_name")
        candidate["nearest_skill_id"] = row["nearest_skill_id"] or candidate.get("nearest_skill_id")
        candidate["nearest_name"] = row["nearest_name"] or candidate.get("nearest_name")
        candidate["nearest_group"] = row["nearest_group"] or candidate.get("nearest_group")
        nearest_id = None
        try:
            nearest_id = int(candidate["nearest_skill_id"]) if candidate.get("nearest_skill_id") else None
        except (TypeError, ValueError):
            nearest_id = None
        candidate["similarity_hint"] = build_similarity_hint(
            match_score_value,
            str(candidate.get("resolution") or ""),
            bool(nearest_id),
            candidate.get("reasons"),
        )
        nearest_preview = load_nearest_skill_preview(conn, nearest_id)
        if nearest_preview:
            candidate["nearest_preview"] = nearest_preview
            candidate["nearest_name"] = candidate.get("nearest_name") or nearest_preview.get("name")
            candidate["nearest_group"] = candidate.get("nearest_group") or nearest_preview.get("group")
        candidate["recommended_action"] = build_candidate_recommended_action(
            match_score_value,
            str(candidate.get("resolution") or ""),
            bool(nearest_id),
            str(candidate.get("nearest_name") or ""),
            candidate.get("reasons"),
            str(candidate.get("decision") or ""),
        )
        candidate["decision_rationale"] = build_decision_rationale(candidate)
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

    if not isinstance(result.get("dag"), dict):
        state = get_brief_dag_state(conn, brief_id)
        result["dag"] = build_deferred_dag_payload(
            state,
            status="waiting_catalog",
            message="DAG строится отдельным шагом после применения проверенных навыков в справочник.",
        )
    if not isinstance(result.get("curriculum_plan"), dict):
        result["curriculum_plan"] = build_deferred_curriculum_plan_payload(
            "УП строится отдельным шагом после применения навыков в справочник, принятия шаблонов и построения DAG."
        )
    result["catalog_state"] = get_brief_catalog_apply_state(conn, brief_id)

    if isinstance(result.get("persisted"), dict):
        result["persisted"]["review_open"] = int(get_brief_dag_state(conn, brief_id)["open_review_count"])
        result["persisted"]["curriculum_plan_rows"] = int(result.get("curriculum_plan", {}).get("row_count", 0) or 0)
        result["persisted"]["catalog_promoted"] = int(result["catalog_state"].get("active_promotions") or 0)
        result["persisted"]["skill_set_items"] = int(result["catalog_state"].get("skill_set_items") or 0)
        result["persisted"]["template_proposals"] = int(result["catalog_state"].get("template_proposals") or 0)
    return result


def build_intake_workflow_steps(
    job: dict[str, object] | None,
    result: dict[str, object] | None,
    dag_build_state: dict[str, object] | None,
) -> list[dict[str, object]]:
    if not job:
        return []

    job_status = str(job.get("status") or "")
    candidates = result.get("candidates") if isinstance(result, dict) else []
    candidates = candidates if isinstance(candidates, list) else []
    accepted_count = len([item for item in candidates if isinstance(item, dict) and item.get("decision") == "accepted"])
    review_count = len([item for item in candidates if isinstance(item, dict) and item.get("decision") == "needs_review"])

    persisted = result.get("persisted") if isinstance(result, dict) and isinstance(result.get("persisted"), dict) else {}
    if isinstance(persisted, dict) and persisted.get("review_open") is not None:
        try:
            review_count = int(persisted.get("review_open") or 0)
        except (TypeError, ValueError):
            pass
    promoted_count = int(persisted.get("catalog_promoted") or 0) if isinstance(persisted, dict) else 0
    template_proposals = int(persisted.get("template_proposals") or 0) if isinstance(persisted, dict) else 0
    catalog_state = result.get("catalog_state") if isinstance(result, dict) and isinstance(result.get("catalog_state"), dict) else {}
    catalog_applied = bool(catalog_state.get("catalog_applied")) if isinstance(catalog_state, dict) else False

    dag_payload = result.get("dag") if isinstance(result, dict) and isinstance(result.get("dag"), dict) else {}
    curriculum_plan = result.get("curriculum_plan") if isinstance(result, dict) and isinstance(result.get("curriculum_plan"), dict) else {}
    dag_nodes = int(dag_payload.get("nodes") or 0) if isinstance(dag_payload, dict) else 0
    plan_id = curriculum_plan.get("plan_id") if isinstance(curriculum_plan, dict) else None

    if job_status in {"pending", "running"}:
        review_status = "active"
        catalog_status = "pending"
        up_status = "pending"
    elif job_status == "failed":
        review_status = "warn"
        catalog_status = "pending"
        up_status = "pending"
    else:
        review_status = "active" if review_count else "done"
        catalog_status = "done" if catalog_applied else ("active" if accepted_count else "pending")
        templates_status = "done" if template_proposals else ("active" if catalog_applied else "pending")
        up_status = "done" if plan_id else ("active" if catalog_applied and template_proposals else "pending")
    if job_status in {"pending", "running", "failed"}:
        templates_status = "pending"

    accepted_atomic = dag_build_state.get("accepted_atomic_count") if isinstance(dag_build_state, dict) else accepted_count
    open_review = dag_build_state.get("open_review_count") if isinstance(dag_build_state, dict) else review_count

    return [
        {
            "key": "brief",
            "label": "Бриф",
            "status": "done",
            "description": "Текст или документ принят в обработку.",
            "href": f"/intake/jobs/{job['id']}",
        },
        {
            "key": "review",
            "label": "Проверка навыков",
            "status": review_status,
            "description": (
                f"Открыто вопросов: {open_review}."
                if review_status == "active"
                else ("Intake завершился ошибкой." if review_status == "warn" else "Кандидаты проверены.")
            ),
            "href": "/reviews" if review_count else f"/intake/jobs/{job['id']}",
        },
        {
            "key": "catalog",
            "label": "Справочник и набор навыков",
            "status": catalog_status,
            "description": f"Принято: {accepted_atomic or accepted_count}, промоций: {promoted_count}.",
            "href": f"/intake/jobs/{job['id']}",
        },
        {
            "key": "templates",
            "label": "Шаблоны УП",
            "status": templates_status,
            "description": f"Предложений: {template_proposals}." if template_proposals else "Появятся после применения навыков в справочник.",
            "href": f"/up/plans/{plan_id}/template-proposals" if plan_id and template_proposals else f"/intake/jobs/{job['id']}",
        },
        {
            "key": "up",
            "label": "DAG и УП",
            "status": up_status,
            "description": "Черновик доступен." if plan_id else "Строится после набора навыков, шаблонов и DAG.",
            "href": f"/up/plans/{plan_id}" if plan_id else "/up",
        },
    ]


def build_intake_workspace_state(
    conn: sqlite3.Connection,
    job: dict[str, object] | None,
    result: dict[str, object] | None,
    dag_build_state: dict[str, object] | None,
) -> dict[str, object]:
    if not job:
        return {"next_step": None, "blockers": [], "catalog_summary": {"total": 0, "items": []}}

    job_id = int(job["id"])
    job_status = str(job.get("status") or "")
    brief_id = result.get("brief_id") if isinstance(result, dict) else None
    brief_id = brief_id if isinstance(brief_id, int) else None

    catalog_state = result.get("catalog_state") if isinstance(result, dict) and isinstance(result.get("catalog_state"), dict) else {}
    curriculum_plan = result.get("curriculum_plan") if isinstance(result, dict) and isinstance(result.get("curriculum_plan"), dict) else {}
    dag_payload = result.get("dag") if isinstance(result, dict) and isinstance(result.get("dag"), dict) else {}
    plan_id = curriculum_plan.get("plan_id") if isinstance(curriculum_plan, dict) else None

    open_skill_reviews = count_open_skill_reviews_for_brief(conn, brief_id) if brief_id is not None else 0
    open_edge_reviews = count_open_prerequisite_edge_reviews_for_brief(conn, brief_id) if brief_id is not None else 0
    open_competency_reviews = count_open_candidate_competencies(conn)
    accepted_atomic = int(catalog_state.get("accepted_atomic") or 0) if isinstance(catalog_state, dict) else 0
    active_promotions = int(catalog_state.get("active_promotions") or 0) if isinstance(catalog_state, dict) else 0
    open_templates = int(catalog_state.get("open_template_proposals") or 0) if isinstance(catalog_state, dict) else 0
    catalog_pending = accepted_atomic > 0 and active_promotions < accepted_atomic
    dag_built = str(dag_payload.get("status") or "").casefold() == "built" and int(dag_payload.get("nodes") or 0) > 0
    plan_ready = bool(plan_id and int(curriculum_plan.get("row_count") or len(curriculum_plan.get("rows") or [])) > 0)
    skills_resolved = job_status not in {"pending", "running", "failed"} and open_skill_reviews == 0

    blockers: list[dict[str, object]] = []
    if open_skill_reviews:
        blockers.append(
            {
                "code": "open_skill_reviews",
                "label": "Открытые навыки",
                "count": open_skill_reviews,
                "severity": "warn",
                "description": "Нужно принять, привязать или отклонить спорные навыки.",
                "href": "/reviews?status=open",
            }
        )
    if catalog_pending:
        blockers.append(
            {
                "code": "catalog_pending",
                "label": "Accepted не применены",
                "count": accepted_atomic - active_promotions,
                "severity": "warn",
                "description": "Принятые навыки ещё не записаны в канонический справочник, синонимы и набор навыков.",
                "href": f"/intake/jobs/{job_id}",
            }
        )
    if open_competency_reviews:
        blockers.append(
            {
                "code": "open_competency_reviews",
                "label": "Кандидатные компетенции",
                "count": open_competency_reviews,
                "severity": "warn",
                "description": "Нужно принять или отклонить новые competency-группировки.",
                "href": "/catalog-admin/candidate-competencies",
            }
        )
    if open_templates:
        blockers.append(
            {
                "code": "open_templates",
                "label": "Шаблоны УП",
                "count": open_templates,
                "severity": "info",
                "description": "Проверьте предложенные шаблоны артефактов перед сборкой УП.",
                "href": f"/up/plans/{plan_id}/template-proposals" if plan_id else "/up",
            }
        )
    if open_edge_reviews:
        blockers.append(
            {
                "code": "open_prerequisite_edges",
                "label": "Рёбра DAG",
                "count": open_edge_reviews,
                "severity": "warn",
                "description": "Проверьте предложенные связи перед финальным использованием графа в УП.",
                "href": "/reviews?status=open&entity_type=prerequisite_edge",
            }
        )
    if not dag_built and not plan_ready and accepted_atomic:
        blockers.append(
            {
                "code": "dag_missing",
                "label": "DAG не построен",
                "count": 1,
                "severity": "info",
                "description": "После проверок нужно построить граф и учебный план.",
                "href": f"/intake/jobs/{job_id}",
            }
        )

    if job_status in {"pending", "running"}:
        next_step = {
            "code": "wait",
            "label": "Дождаться обработки",
            "description": "Intake-задача ещё выполняется.",
            "method": "get",
            "href": f"/intake/jobs/{job_id}",
            "disabled": True,
        }
    elif job_status == "failed":
        next_step = {
            "code": "failed",
            "label": "Посмотреть ошибку",
            "description": "Pipeline завершился ошибкой.",
            "method": "get",
            "href": f"/intake/jobs/{job_id}",
            "disabled": True,
        }
    elif open_skill_reviews:
        next_step = {
            "code": "open_reviews",
            "label": "Открыть проверку навыков",
            "description": f"Осталось спорных навыков: {open_skill_reviews}.",
            "method": "get",
            "href": "/reviews?status=open",
        }
    elif catalog_pending:
        next_step = {
            "code": "apply_catalog",
            "label": "Применить принятые навыки в справочник",
            "description": f"Будет применено: {accepted_atomic - active_promotions}.",
            "method": "post",
            "href": f"/intake/jobs/{job_id}/next-step",
        }
    elif open_competency_reviews:
        next_step = {
            "code": "candidate_competencies",
            "label": "Проверить кандидатные компетенции",
            "description": f"Открыто competency-группировок: {open_competency_reviews}.",
            "method": "get",
            "href": "/catalog-admin/candidate-competencies",
        }
    elif open_templates and plan_id:
        next_step = {
            "code": "templates",
            "label": "Проверить шаблоны УП",
            "description": f"Открыто предложений: {open_templates}.",
            "method": "get",
            "href": f"/up/plans/{plan_id}/template-proposals",
        }
    elif open_edge_reviews:
        next_step = {
            "code": "review_dag_edges",
            "label": "Проверить рёбра DAG",
            "description": f"Открыто связей на проверке: {open_edge_reviews}.",
            "method": "get",
            "href": "/reviews?status=open&entity_type=prerequisite_edge",
        }
    elif not plan_ready:
        next_step = {
            "code": "build_dag",
            "label": "Построить DAG и УП",
            "description": "Собрать граф и учебный план из принятого набора навыков.",
            "method": "post",
            "href": f"/intake/jobs/{job_id}/next-step",
        }
    else:
        next_step = {
            "code": "open_up",
            "label": "Открыть учебный план",
            "description": "Черновик УП готов к проверке.",
            "method": "get",
            "href": f"/up/plans/{plan_id}",
        }

    return {
        "brief_id": brief_id,
        "next_step": next_step,
        "blockers": blockers,
        "catalog_summary": load_brief_catalog_promotion_summary(conn, brief_id) if brief_id is not None else {"total": 0, "items": []},
        "open_skill_reviews": open_skill_reviews,
        "open_edge_reviews": open_edge_reviews,
        "open_competency_reviews": open_competency_reviews,
        "catalog_pending": catalog_pending,
        "skills_resolved": skills_resolved,
        "dag_built": dag_built,
        "plan_ready": plan_ready,
        "show_downstream_sections": skills_resolved
        and (
            dag_built
            or plan_ready
            or (
                not catalog_pending
                and not open_competency_reviews
                and not open_templates
            )
        ),
    }


def apply_candidate_decision(
    conn: sqlite3.Connection,
    suggestion_id: int,
    target_decision: str,
    resolution_note: str | None = None,
) -> int | None:
    from spravochnik_intake.pipeline import storage

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
    if target_decision != "accepted":
        storage.revert_suggestion_promotion(conn, suggestion_id)
    clear_brief_dag_artifacts(conn, brief_id)
    clear_brief_curriculum_plan_artifacts(conn, brief_id)
    catalog_state = get_brief_catalog_apply_state(conn, brief_id)
    update_jobs_catalog_payload(
        conn,
        brief_id,
        catalog_state=catalog_state,
        persisted_update={
            "catalog_promoted": int(catalog_state.get("active_promotions") or 0),
            "skill_set_items": int(catalog_state.get("skill_set_items") or 0),
            "curriculum_plan_rows": 0,
        },
    )
    update_jobs_curriculum_plan_payload(
        conn,
        brief_id,
        build_deferred_curriculum_plan_payload(
            "УП инвалидирован изменением решения по skill. Примените решения в справочник и заново постройте DAG/УП."
        ),
        persisted_update={"curriculum_plan_rows": 0},
    )
    conn.commit()
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
            s.canonical_name,
            ss.confidence,
            ss.council_agreement
        FROM skill_suggestion ss
        LEFT JOIN skill s ON s.id = ss.canonical_skill_id
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
            canonical_name=row["canonical_name"],
            canonical_group=None,
            decision="accepted",
        )
        cands.append(candidate)
        tmp_to_db[tmp_id] = int(row["id"])
    return cands, tmp_to_db


def load_brief_spec_for_plan(conn: sqlite3.Connection, brief_id: int) -> dict[str, object]:
    row = conn.execute(
        "SELECT raw_text, role, seniority, domain FROM profile_brief WHERE id = ?",
        (brief_id,),
    ).fetchone()
    if not row:
        return {}
    from spravochnik_intake.pipeline import stage_brief_to_catalog
    from spravochnik_intake.pipeline import storage as intake_storage

    spec = {
        "role": row["role"],
        "seniority": row["seniority"],
        "domain": row["domain"],
    }
    spec.update({key: value for key, value in stage_brief_to_catalog.extract_workload_from_text(str(row["raw_text"] or "")).items() if value is not None})
    spec["artifact_templates"] = intake_storage.load_curriculum_artifact_templates(conn)
    return spec


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
    template_stats = count_brief_template_proposals(conn, brief_id)
    plan_payload["template_proposal_count"] = template_stats["total"]
    plan_payload["template_proposal_status"] = "open" if template_stats["open"] else ("done" if template_stats["total"] else "none")
    conn.execute(
        "UPDATE curriculum_plan SET payload_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (json.dumps(plan_payload, ensure_ascii=False), int(save_meta["plan_id"])),
    )
    conn.commit()
    update_jobs_curriculum_plan_payload(
        conn,
        brief_id,
        plan_payload,
        persisted_update={
            "curriculum_plan_rows": save_meta["row_count"],
            "template_proposals": template_stats["total"],
        },
    )
    return plan_payload


def build_dag_for_brief(conn: sqlite3.Connection, brief_id: int) -> dict[str, object]:
    from spravochnik_intake.pipeline import llm as intake_llm
    from spravochnik_intake.pipeline import stage_catalog_to_dag, storage

    catalog_state = get_brief_catalog_apply_state(conn, brief_id)
    if not bool(catalog_state.get("catalog_applied")):
        clear_brief_dag_artifacts(conn, brief_id)
        clear_brief_curriculum_plan_artifacts(conn, brief_id)
        state = refresh_brief_dag_state(
            conn,
            brief_id,
            status="waiting_catalog",
            message="DAG не построен: сначала примените принятые навыки в справочник и набор навыков.",
        )
        plan_payload = build_deferred_curriculum_plan_payload(
            "УП не построен: сначала примените принятые skills в справочник, затем примите шаблоны и запустите DAG."
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
            "catalog_state": catalog_state,
            "dag": build_deferred_dag_payload(
                state,
                status="waiting_catalog",
                message="DAG не построен: сначала примените принятые навыки в справочник и набор навыков.",
            ),
            "curriculum_plan": plan_payload,
        }

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

    intake_llm.set_usage_context(stage="dag", brief_id=brief_id)
    try:
        edges, dag, removed_cycle, removed_transitive, dag_payload = stage_catalog_to_dag.run(
            cands,
            edge_decisions=load_prerequisite_edge_decisions(conn, brief_id),
        )
    finally:
        intake_llm.set_usage_context(stage=None)
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


def ensure_catalog_admin_runtime_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_group (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 999,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'candidate', 'deprecated')),
            source TEXT NOT NULL DEFAULT 'derived' CHECK (source IN ('live_snapshot', 'manual', 'derived')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS indicator (
            id INTEGER PRIMARY KEY,
            skill_id INTEGER NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
            indicator_type TEXT NOT NULL,
            text TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 999,
            complexity_band TEXT,
            complexity_label TEXT,
            complexity_sort_order INTEGER,
            source_indicator_row_id INTEGER,
            source_profile_name TEXT,
            source_scale_title TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            UNIQUE (skill_id, indicator_type, normalized_text)
        )
        """
    )
    skill_columns = {
        "group_id": "INTEGER REFERENCES skill_group(id) ON DELETE SET NULL",
        "code": "TEXT",
        "name": "TEXT",
        "sort_order": "INTEGER NOT NULL DEFAULT 999",
        "complexity_min_band": "TEXT",
        "complexity_max_band": "TEXT",
        "complexity_summary": "TEXT",
        "source_scale_title": "TEXT",
        "description": "TEXT",
        "source_skill_id": "INTEGER",
        "source_skill_name": "TEXT",
        "resolution_status": "TEXT NOT NULL DEFAULT 'matched'",
        "match_note": "TEXT",
        "is_active": "INTEGER NOT NULL DEFAULT 1",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }
    indicator_columns = {
        "complexity_band": "TEXT",
        "complexity_label": "TEXT",
        "complexity_sort_order": "INTEGER",
        "source_scale_title": "TEXT",
    }

    if table_exists(conn, "skill") and not column_exists(conn, "skill", "group_id"):
        conn.execute("ALTER TABLE skill ADD COLUMN group_id INTEGER REFERENCES skill_group(id) ON DELETE SET NULL")

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

        fallback_group_id = ensure_catalog_group(conn, "uncategorized", "Прочие навыки", 9999, "active", "derived")
        if column_exists(conn, "skill", "canonical_name"):
            conn.execute("UPDATE skill SET name = canonical_name WHERE name IS NULL OR TRIM(name) = ''")
        conn.execute("UPDATE skill SET code = 'skill-' || id WHERE code IS NULL OR TRIM(code) = ''")
        conn.execute("UPDATE skill SET group_id = ? WHERE group_id IS NULL", (fallback_group_id,))
        conn.execute("UPDATE skill SET is_active = CASE WHEN status = 'deprecated' THEN 0 ELSE 1 END WHERE is_active IS NULL")
        conn.execute("UPDATE skill SET resolution_status = 'matched' WHERE resolution_status IS NULL OR TRIM(resolution_status) = ''")

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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_code ON skill(code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_active_status ON skill(status, is_active)")

    if table_exists(conn, "skill") and table_exists(conn, "indicator"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_indicator_skill_active
            ON indicator(skill_id, is_active, sort_order)
            """
        )
        for row in conn.execute("SELECT id FROM skill ORDER BY id"):
            refresh_catalog_skill_complexity(conn, row["id"], commit=False)
    conn.commit()


def ensure_catalog_group(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    sort_order: int,
    status: str = "active",
    source: str = "derived",
) -> int:
    row = conn.execute(
        "SELECT id FROM skill_group WHERE code = ? OR name = ? ORDER BY id LIMIT 1",
        (code, name),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE skill_group
            SET code = ?,
                name = ?,
                sort_order = ?,
                status = ?,
                source = COALESCE(NULLIF(source, ''), ?),
                updated_at = ?
            WHERE id = ?
            """,
            (code, name, sort_order, status, source, utc_now_iso(), int(row["id"])),
        )
        return int(row["id"])
    cursor = conn.execute(
        """
        INSERT INTO skill_group(code, name, sort_order, status, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (code, name, sort_order, status, source, utc_now_iso()),
    )
    return int(cursor.lastrowid)


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
    intake_job_id: int | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, object]:
    from spravochnik_intake.pipeline import llm as intake_llm
    from spravochnik_intake.pipeline import stage_brief_to_catalog, stage_normalize, storage
    from spravochnik_intake.pipeline import config as intake_config
    from spravochnik_intake.pipeline.catalog_repo import CatalogRepo

    ensure_intake_runtime_schema(conn, db_path)

    def notify(stage: str, note: str) -> None:
        if progress_callback:
            progress_callback(stage, note)

    repo = CatalogRepo(str(db_path))
    try:
        intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="decompose")
        notify("decompose", "Декомпозиция свободного брифа в роль, уровень и поисковые подзапросы.")
        spec = stage_brief_to_catalog.decompose(brief_text)

        intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="draft")
        notify("draft", "Черновик навыков из брифа без внешнего поиска.")
        raw_candidates, coverage = stage_brief_to_catalog.synthesize_draft_from_brief(brief_text, spec)

        intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="atomize")
        notify("atomize", "Проверка атомарности кандидатов, разбиение составных формулировок и реклассификация не-навыков.")
        atomized_candidates = stage_brief_to_catalog.atomize_candidates(raw_candidates, spec)

        intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="normalize")
        notify("normalize", "Нормализация названий и безопасное схлопывание дублирующих atomic skills.")
        candidates, normalize_report = stage_normalize.run(atomized_candidates, spec)

        intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="resolve")
        notify("resolve", "Сопоставление навыков-кандидатов с текущим каталогом.")
        evidence = []
        stage_brief_to_catalog.resolve_candidates(candidates, evidence, repo)

        gray_candidates = stage_brief_to_catalog.select_evidence_enrichment_candidates(candidates)
        if gray_candidates:
            intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="search")
            notify("search", f"Сбор external evidence только для серой зоны: {len(gray_candidates)} кандидатов.")
            evidence = stage_brief_to_catalog.gather_evidence_for_gray_zone(candidates, spec, cache_conn=conn)
            intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="resolve")
            notify("resolve", "Повторный резолв после evidence enrichment серой зоны.")
            stage_brief_to_catalog.resolve_candidates(candidates, evidence, repo)
        else:
            notify("search", "Внешний поиск не потребовался: кандидаты закрылись текущим каталогом.")

        coverage = stage_brief_to_catalog.build_coverage_audit(spec, candidates, normalize_report=normalize_report)
        council_metrics_preview = {
            "sent_to_council": len(stage_brief_to_catalog.select_council_candidates(candidates)),
        }
        if intake_config.USE_COUNCIL and council_metrics_preview["sent_to_council"] > 0:
            intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="council")
            notify(
                "council",
                f"Экспертное жюри проверяет спорные навыки: {council_metrics_preview['sent_to_council']} кандидатов.",
            )
            stage_brief_to_catalog.run_council(candidates)
        else:
            notify("council", "Council не потребовался: спорных навыков для panel нет.")
        intake_llm.set_usage_context(job_id=intake_job_id, brief_id=None, stage="triage")
        notify("triage", "Финальный триаж: что принять автоматически, а что отправить на review.")
        stage_brief_to_catalog.triage_candidates(candidates, spec)
        candidate_metrics = stage_brief_to_catalog.build_candidate_metrics(candidates)
    finally:
        intake_llm.clear_usage_context()
        repo.close()

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

    notify("ready_for_review", "Intake-анализ завершён. Методолог принимает skills, затем явно применяет решения в справочник.")
    dag_state = get_brief_dag_state(conn, brief_id)
    dag_payload = build_deferred_dag_payload(
        dag_state,
        status="waiting_catalog",
        message="DAG ещё не строился: сначала завершите проверку skills и примените решения в справочник.",
    )
    curriculum_plan = build_deferred_curriculum_plan_payload(
        "УП ещё не строился: сначала примените проверенные skills в справочник, примите шаблоны УП и постройте DAG.",
        audience_level=str(spec.get("seniority") or "Начальный"),
    )

    return {
        "brief_id": brief_id,
        "spec": spec,
        "candidates": [
            {
                "name": candidate.name,
                "source_name": candidate.source_name,
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
                "match_score": format_catalog_similarity(candidate.match_score)[0],
                "novelty_score": format_catalog_similarity(candidate.match_score)[1],
                "nearest_skill_id": candidate.nearest_skill_id,
                "nearest_name": candidate.nearest_name,
                "nearest_group": candidate.nearest_group,
                "similarity_hint": build_similarity_hint(candidate.match_score, candidate.resolution, bool(candidate.nearest_skill_id), candidate.reasons),
                "recommended_action": build_candidate_recommended_action(
                    candidate.match_score,
                    candidate.resolution,
                    bool(candidate.nearest_skill_id),
                    candidate.nearest_name,
                    candidate.reasons,
                    candidate.decision,
                ),
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
            "catalog_promoted": 0,
            "catalog_reverted": 0,
            "template_proposals": 0,
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

        result = run_intake_pipeline(
            conn,
            db_path,
            str(job["brief_text"]),
            intake_job_id=job_id,
            progress_callback=progress,
        )
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


def refresh_catalog_skill_complexity(conn: sqlite3.Connection, skill_id: int, commit: bool = True) -> None:
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
    from spravochnik_intake.pipeline import competency_catalog
    from spravochnik_intake.pipeline import storage

    repair_intake_review_links(conn)
    review_row = conn.execute(
        """
        SELECT id, entity_type, entity_id, source_ref, reason_code, details
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
    details = parse_review_details_json(review_row["details"])
    rebuild_after_review = False
    if review_row["entity_type"] == "competency" and suggestion_id:
        competency_id = int(suggestion_id)
        if new_status == "resolved":
            competency_catalog.resolve_competency_candidate(conn, competency_id=competency_id, accepted=True)
        elif new_status == "ignored":
            competency_catalog.resolve_competency_candidate(conn, competency_id=competency_id, accepted=False)
        else:
            competency_catalog.reopen_competency_candidate(conn, competency_id=competency_id)
    elif brief_id is not None and details.get("review_kind") == "prerequisite_edge":
        if new_status in {"resolved", "ignored"}:
            save_prerequisite_edge_decision(
                conn,
                brief_id=brief_id,
                details=details,
                decision="accepted" if new_status == "resolved" else "rejected",
                resolution_note=resolution_note,
            )
        elif table_exists(conn, "prerequisite_edge_decision"):
            conn.execute(
                "DELETE FROM prerequisite_edge_decision WHERE brief_id = ? AND edge_key = ?",
                (brief_id, str(details.get("edge_key") or "")),
            )
        clear_brief_dag_artifacts(conn, brief_id)
    elif suggestion_id and brief_id is not None:
        mapped_decision = "needs_review"
        if new_status == "resolved":
            mapped_decision = "accepted"
        elif new_status == "ignored":
            mapped_decision = "rejected"
        conn.execute(
            "UPDATE skill_suggestion SET decision = ? WHERE id = ?",
            (mapped_decision, suggestion_id),
        )
        if mapped_decision == "accepted":
            storage.promote_suggestion_to_catalog(conn, suggestion_id)
        else:
            storage.revert_suggestion_promotion(conn, suggestion_id)
        clear_brief_dag_artifacts(conn, brief_id)
        rebuild_after_review = True
    conn.commit()
    if brief_id is not None and rebuild_after_review:
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
        "invalid": "Невалиден",
    }
    return mapping.get((status or "").strip().casefold(), "Неизвестно")


def weighted_skills_from_row(row: dict[str, object]) -> str:
    existing = str(row.get("weighted_skills") or "").strip()
    if existing:
        return existing
    skills = [
        item.strip()
        for item in str(row.get("skills_list") or "").split(",")
        if item.strip()
    ]
    if not skills:
        return ""
    base_weight = round(100 / len(skills))
    weights = [base_weight] * len(skills)
    weights[-1] += 100 - sum(weights)
    return ", ".join(f"{skill}: {weight}%" for skill, weight in zip(skills, weights, strict=False))


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
                row.get("outcomes_know", ""),
                row.get("outcomes_can", ""),
                row.get("outcomes_skills", ""),
                row.get("required_tools", ""),
                row.get("materials", ""),
                row.get("storytelling", ""),
                row.get("delivery_format", ""),
                row.get("group_size", ""),
                row.get("effort_hours", ""),
                row.get("effort_days", ""),
                row.get("cumulative_days", ""),
                row.get("xp", ""),
                row.get("completion_percent", ""),
                row.get("p2p_checks", ""),
                weighted_skills_from_row(row),
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


def _count_up_outcomes(row: dict[str, object]) -> int:
    total = 0
    for key in ("outcomes_know", "outcomes_can", "outcomes_skills"):
        total += len([line for line in str(row.get(key) or "").splitlines() if line.strip()])
    return total


def _count_up_skills(row: dict[str, object]) -> int:
    raw_node_ids = row.get("node_ids")
    if isinstance(raw_node_ids, list):
        return len(raw_node_ids)
    raw_skills = str(row.get("skills_list") or "").strip()
    if not raw_skills:
        return 0
    return len([item for item in raw_skills.split(",") if item.strip()])


def build_curriculum_quality_metrics_for_ui(
    rows: list[dict[str, object]],
    raw_metrics: dict[str, object] | None,
) -> dict[str, object]:
    metrics = dict(raw_metrics or {})
    project_count = len(rows)
    skill_counts = [_count_up_skills(row) for row in rows]
    primary_skill_counts = [int(row.get("primary_skill_count", _count_up_skills(row)) or 0) for row in rows]
    repeat_skill_counts = [int(row.get("repeat_skill_count", 0) or 0) for row in rows]
    outcome_counts = [_count_up_outcomes(row) for row in rows]
    target_skills = metrics.get("target_skills_per_project") if isinstance(metrics.get("target_skills_per_project"), list) else []
    target_outcomes = metrics.get("target_outcomes_per_project") if isinstance(metrics.get("target_outcomes_per_project"), list) else []
    max_skills = int(target_skills[-1]) if target_skills else 0
    max_outcomes = int(target_outcomes[-1]) if target_outcomes else 0
    enriched_project_count = sum(
        1
        for row in rows
        if all(
            str(row.get(field) or "").strip()
            for field in (
                "project_summary",
                "artifact",
                "materials",
                "storytelling",
                "validation_criteria",
                "delivery_format",
            )
        )
    )
    artifact_field_count = sum(1 for row in rows if str(row.get("artifact") or "").strip())
    validation_criteria_count = sum(1 for row in rows if str(row.get("validation_criteria") or "").strip())
    if project_count:
        metrics["avg_skills_per_project"] = round(sum(skill_counts) / project_count, 2)
        metrics["avg_primary_skills_per_project"] = round(sum(primary_skill_counts) / project_count, 2)
        metrics["avg_repeat_skills_per_project"] = round(sum(repeat_skill_counts) / project_count, 2)
        metrics["avg_outcomes_per_project"] = round(sum(outcome_counts) / project_count, 2)
        metrics["single_skill_project_count"] = sum(1 for count in skill_counts if count <= 1)
        metrics["overloaded_project_count"] = sum(
            1
            for skill_count, outcome_count in zip(skill_counts, outcome_counts, strict=False)
            if (max_skills and skill_count > max_skills) or (max_outcomes and outcome_count > max_outcomes)
        )
        metrics["enriched_project_count"] = enriched_project_count
        metrics["enrichment_completeness_pct"] = round(enriched_project_count / project_count * 100, 1)
        metrics["artifact_field_count"] = artifact_field_count
        metrics["validation_criteria_count"] = validation_criteria_count
    else:
        metrics.setdefault("avg_skills_per_project", 0.0)
        metrics.setdefault("avg_primary_skills_per_project", 0.0)
        metrics.setdefault("avg_repeat_skills_per_project", 0.0)
        metrics.setdefault("avg_outcomes_per_project", 0.0)
        metrics.setdefault("single_skill_project_count", 0)
        metrics.setdefault("overloaded_project_count", 0)
        metrics.setdefault("enriched_project_count", 0)
        metrics.setdefault("enrichment_completeness_pct", 0.0)
        metrics.setdefault("artifact_field_count", 0)
        metrics.setdefault("validation_criteria_count", 0)
    metrics.setdefault("core_thread_count", 0)
    metrics.setdefault("repeated_thread_count", 0)
    metrics.setdefault("spiral_enabled", False)
    metrics.setdefault("artifact_project_count", 0)
    metrics.setdefault("db_template_project_count", 0)
    metrics.setdefault("unassigned_node_count", 0)
    return metrics


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

    payload_rows_by_number: dict[int, dict[str, object]] = {}
    if isinstance(payload.get("rows"), list):
        for payload_row in payload.get("rows") or []:
            if not isinstance(payload_row, dict):
                continue
            row_number = int(payload_row.get("row_number", 0) or 0)
            if row_number:
                payload_rows_by_number[row_number] = payload_row

    normalized_rows: list[dict[str, object]] = []
    for source_row in rows:
        row = dict(source_row)
        payload_row = payload_rows_by_number.get(int(row.get("row_number", 0) or 0), {})
        for transient_key in ("node_ids", "node_names", "primary_skill_count", "repeat_skill_count", "occurrence_count", "outcome_count"):
            if transient_key in payload_row and transient_key not in row:
                row[transient_key] = payload_row[transient_key]
        if not any(row.get(key) for key in ("outcomes_know", "outcomes_can", "outcomes_skills")) and row.get("learning_outcomes"):
            row["outcomes_can"] = row.get("learning_outcomes")
        row.setdefault("materials", "")
        row["weighted_skills"] = weighted_skills_from_row(row)
        normalized_rows.append(row)
    rows = normalized_rows

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
    payload_report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    raw_quality_metrics = payload_report.get("quality_metrics") if isinstance(payload_report.get("quality_metrics"), dict) else {}
    report = {
        "coverage_ok": bool(payload_report.get("coverage_ok", False)),
        "order_violations": payload_report.get("order_violations") if isinstance(payload_report.get("order_violations"), list) else [],
        "project_violations": payload_report.get("project_violations") if isinstance(payload_report.get("project_violations"), list) else [],
        "quality_metrics": build_curriculum_quality_metrics_for_ui(rows, raw_quality_metrics),
    }

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
        "template_proposal_count": int(payload.get("template_proposal_count") or 0),
        "template_proposal_status": str(payload.get("template_proposal_status") or "none"),
        "csv_primary_header": payload.get("csv_primary_header") or CSV_PRIMARY_HEADER,
        "csv_secondary_header": payload.get("csv_secondary_header") or CSV_SECONDARY_HEADER,
        "report": report,
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
    if table_exists(conn, "curriculum_artifact_template_proposal"):
        proposal_stats = conn.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count
            FROM curriculum_artifact_template_proposal
            WHERE brief_id = ?
            """,
            (int(plan_meta.get("brief_id") or 0),),
        ).fetchone()
        if proposal_stats:
            total_count = int(proposal_stats["total_count"] or 0)
            open_count = int(proposal_stats["open_count"] or 0)
            plan_payload["template_proposal_count"] = total_count
            plan_payload["template_proposal_status"] = "open" if open_count else ("done" if total_count else "none")
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
            project_name, project_summary, outcomes_know, outcomes_can, outcomes_skills,
            learning_outcomes, skills_list, audience_level, required_tools, materials,
            validation_criteria, storytelling, delivery_format, group_size, effort_hours, effort_days,
            cumulative_days, xp, platform_project_name, artifact_links
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "",
            "",
            "",
            plan.get("audience_level", "Начальный"),
            "",
            "",
            "",
            "",
            "индивидуальный",
            1,
            0.0,
            None,
            None,
            None,
            "",
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


def parse_artifact_template_scopes(form_data: dict[str, str]) -> list[dict[str, object]]:
    scope_type = form_data.get("scope_type", "coverage_area").strip() or "coverage_area"
    raw_names = form_data.get("scope_names", "").strip()
    weight = parse_optional_float(form_data.get("scope_weight")) or 1.0
    if scope_type == "any":
        return [{"scope_type": "any", "scope_name": "*", "weight": weight}]
    names = [
        item.strip()
        for item in re.split(r"[\n;]+", raw_names)
        if item.strip()
    ]
    return [{"scope_type": scope_type, "scope_name": name, "weight": weight} for name in names]


def parse_scope_names(raw_names: str | None, scope_type: str = "coverage_area") -> list[str]:
    if scope_type == "any":
        return ["*"]
    return [item.strip() for item in re.split(r"[\n;]+", raw_names or "") if item.strip()]


def update_curriculum_plan_row(conn: sqlite3.Connection, plan_id: int, row_id: int, form_data: dict[str, str]) -> dict[str, object]:
    row = get_curriculum_plan_row(conn, plan_id, row_id)
    if not row:
        raise ValueError("Curriculum plan row not found")
    outcomes_know = form_data.get("outcomes_know", "").strip()
    outcomes_can = form_data.get("outcomes_can", "").strip()
    outcomes_skills = form_data.get("outcomes_skills", "").strip()
    learning_outcomes = "\n".join(item for item in [outcomes_know, outcomes_can, outcomes_skills] if item)
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
            outcomes_know = ?,
            outcomes_can = ?,
            outcomes_skills = ?,
            learning_outcomes = ?,
            skills_list = ?,
            audience_level = ?,
            required_tools = ?,
            materials = ?,
            validation_criteria = ?,
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
            outcomes_know,
            outcomes_can,
            outcomes_skills,
            learning_outcomes,
            form_data.get("skills_list", "").strip(),
            form_data.get("audience_level", "").strip(),
            form_data.get("required_tools", "").strip(),
            form_data.get("materials", "").strip(),
            form_data.get("validation_criteria", "").strip(),
            form_data.get("storytelling", "").strip(),
            form_data.get("delivery_format", "").strip(),
            form_data.get("group_size", "").strip(),
            parse_optional_float(form_data.get("effort_hours")) or 0.0,
            None,
            None,
            None,
            "",
            "",
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


def reset_curriculum_plan_payload_in_jobs(conn: sqlite3.Connection, brief_id: int, message: str) -> None:
    if not table_exists(conn, "intake_job"):
        return
    rows = conn.execute(
        """
        SELECT id, result_payload
        FROM intake_job
        WHERE result_payload IS NOT NULL
          AND json_valid(result_payload)
          AND json_extract(result_payload, '$.brief_id') = ?
        """,
        (brief_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["result_payload"] or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload["curriculum_plan"] = build_deferred_curriculum_plan_payload(message)
        persisted = payload.get("persisted")
        if isinstance(persisted, dict):
            persisted["curriculum_plan_rows"] = 0
        conn.execute(
            "UPDATE intake_job SET result_payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), datetime.now(UTC).isoformat(), row["id"]),
        )


def delete_curriculum_plan(conn: sqlite3.Connection, plan_id: int) -> bool:
    if not table_exists(conn, "curriculum_plan"):
        return False
    row = conn.execute("SELECT id, brief_id FROM curriculum_plan WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return False
    brief_id = row["brief_id"]
    if table_exists(conn, "curriculum_plan_row"):
        conn.execute("DELETE FROM curriculum_plan_row WHERE plan_id = ?", (plan_id,))
    conn.execute("DELETE FROM curriculum_plan WHERE id = ?", (plan_id,))
    if isinstance(brief_id, int):
        reset_curriculum_plan_payload_in_jobs(conn, brief_id, "УП был удалён вручную.")
    conn.commit()
    return True


def cleanup_empty_curriculum_plans(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "curriculum_plan"):
        return 0
    rows = conn.execute(
        """
        SELECT cp.id
        FROM curriculum_plan cp
        WHERE cp.status = 'deferred'
           OR cp.total_projects = 0
           OR NOT EXISTS (
                SELECT 1
                FROM curriculum_plan_row cpr
                WHERE cpr.plan_id = cp.id
           )
        """
    ).fetchall()
    deleted = 0
    for row in rows:
        if delete_curriculum_plan(conn, int(row["id"])):
            deleted += 1
    return deleted


def clear_intake_workspace(conn: sqlite3.Connection) -> dict[str, int]:
    """Clear transient intake artifacts while keeping canonical catalog tables intact."""
    from spravochnik_intake.pipeline import competency_catalog
    from spravochnik_intake.pipeline import storage as intake_storage

    stats: dict[str, int] = {}
    intake_competency_ids: set[int] = set()

    if table_exists(conn, "review_queue"):
        intake_competency_ids.update(
            int(row["entity_id"])
            for row in conn.execute(
                """
                SELECT entity_id
                FROM review_queue
                WHERE entity_type = 'competency'
                  AND entity_id IS NOT NULL
                  AND source_ref LIKE 'intake_accept:%'
                """
            ).fetchall()
        )

    if table_exists(conn, "profile_competency") and table_exists(conn, "profile"):
        intake_competency_ids.update(
            int(row["competency_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT pc.competency_id
                FROM profile_competency pc
                JOIN profile p ON p.id = pc.profile_id
                WHERE p.slug = ?
                """,
                (competency_catalog.SERVICE_PROFILE_SLUG,),
            ).fetchall()
            if row["competency_id"] is not None
        )

    if table_exists(conn, "skill_promotion_log"):
        active_promotions = conn.execute(
            """
            SELECT suggestion_id
            FROM skill_promotion_log
            WHERE status = 'active'
            ORDER BY id
            """
        ).fetchall()
        reverted = 0
        for row in active_promotions:
            result = intake_storage.revert_suggestion_promotion(conn, int(row["suggestion_id"]))
            if result.get("status") == "reverted":
                reverted += 1
        stats["skill_promotions_reverted"] = reverted

    if table_exists(conn, "indicator"):
        indicator_cols = table_columns(conn, "indicator")
        clauses = []
        params: list[object] = []
        if "source_scale_title" in indicator_cols:
            clauses.append("source_scale_title = 'intake-live'")
        if "source_profile_name" in indicator_cols:
            clauses.append("source_profile_name = ?")
            params.append(competency_catalog.SERVICE_PROFILE_NAME)
        if clauses:
            stats["indicator_intake"] = conn.execute(
                f"DELETE FROM indicator WHERE {' OR '.join(clauses)}",
                tuple(params),
            ).rowcount

    if table_exists(conn, "indicator_level_cell") and table_exists(conn, "indicator_row"):
        stats["indicator_level_cell_intake"] = conn.execute(
            """
            DELETE FROM indicator_level_cell
            WHERE indicator_row_id IN (
                SELECT id FROM indicator_row WHERE COALESCE(notes, '') LIKE 'intake_accept:%'
            )
            """
        ).rowcount

    if table_exists(conn, "indicator_row"):
        stats["indicator_row_intake"] = conn.execute(
            "DELETE FROM indicator_row WHERE COALESCE(notes, '') LIKE 'intake_accept:%'"
        ).rowcount

    if table_exists(conn, "competency_skill") and table_exists(conn, "profile_competency") and table_exists(conn, "profile"):
        stats["competency_skill_intake"] = conn.execute(
            """
            DELETE FROM competency_skill
            WHERE id IN (
                SELECT cs.id
                FROM competency_skill cs
                JOIN profile_competency pc ON pc.id = cs.profile_competency_id
                JOIN profile p ON p.id = pc.profile_id
                WHERE p.slug = ?
            )
            """,
            (competency_catalog.SERVICE_PROFILE_SLUG,),
        ).rowcount

    if table_exists(conn, "profile_competency") and table_exists(conn, "profile"):
        stats["profile_competency_intake_orphan"] = conn.execute(
            """
            DELETE FROM profile_competency
            WHERE id IN (
                SELECT pc.id
                FROM profile_competency pc
                JOIN profile p ON p.id = pc.profile_id
                WHERE p.slug = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM competency_skill cs WHERE cs.profile_competency_id = pc.id
                  )
            )
            """,
            (competency_catalog.SERVICE_PROFILE_SLUG,),
        ).rowcount

    if table_exists(conn, "profile"):
        stats["profile_intake_empty"] = conn.execute(
            """
            DELETE FROM profile
            WHERE slug = ?
              AND NOT EXISTS (
                  SELECT 1 FROM profile_competency pc WHERE pc.profile_id = profile.id
              )
            """,
            (competency_catalog.SERVICE_PROFILE_SLUG,),
        ).rowcount

    if intake_competency_ids and table_exists(conn, "competency") and table_exists(conn, "profile_competency"):
        ordered_intake_competency_ids = sorted(intake_competency_ids)
        placeholders = ", ".join("?" for _ in ordered_intake_competency_ids)
        stats["competency_intake_candidate_orphan"] = conn.execute(
            f"""
            DELETE FROM competency
            WHERE id IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM profile_competency pc WHERE pc.competency_id = competency.id
              )
            """,
            tuple(ordered_intake_competency_ids),
        ).rowcount

    if table_exists(conn, "review_queue"):
        stats["review_queue"] = conn.execute(
            """
            DELETE FROM review_queue
            WHERE source_ref LIKE 'brief:%'
               OR source_ref LIKE 'intake_accept:%'
            """
        ).rowcount

    if table_exists(conn, "skill_prerequisite") and column_exists(conn, "skill_prerequisite", "brief_id"):
        stats["skill_prerequisite"] = conn.execute("DELETE FROM skill_prerequisite WHERE brief_id IS NOT NULL").rowcount

    if table_exists(conn, "prerequisite_edge_decision") and column_exists(conn, "prerequisite_edge_decision", "brief_id"):
        stats["prerequisite_edge_decision"] = conn.execute(
            "DELETE FROM prerequisite_edge_decision WHERE brief_id IS NOT NULL"
        ).rowcount

    if table_exists(conn, "skill_set") and table_exists(conn, "skill_set_item"):
        runtime_skill_sets = conn.execute(
            """
            SELECT id
            FROM skill_set
            WHERE source_type IN ('brief', 'curriculum_plan')
               OR source_ref LIKE 'brief:%'
               OR source_ref LIKE '%;brief:%'
            """
        ).fetchall()
        runtime_skill_set_ids = [int(row["id"]) for row in runtime_skill_sets]
        if runtime_skill_set_ids:
            placeholders = ", ".join("?" for _ in runtime_skill_set_ids)
            stats["skill_set_item_runtime"] = conn.execute(
                f"DELETE FROM skill_set_item WHERE skill_set_id IN ({placeholders})",
                tuple(runtime_skill_set_ids),
            ).rowcount
            stats["skill_set_runtime"] = conn.execute(
                f"DELETE FROM skill_set WHERE id IN ({placeholders})",
                tuple(runtime_skill_set_ids),
            ).rowcount

    if table_exists(conn, "curriculum_plan_row") and table_exists(conn, "curriculum_plan"):
        stats["curriculum_plan_row"] = conn.execute(
            """
            DELETE FROM curriculum_plan_row
            WHERE plan_id IN (SELECT id FROM curriculum_plan WHERE brief_id IS NOT NULL)
            """
        ).rowcount

    if table_exists(conn, "curriculum_plan"):
        stats["curriculum_plan"] = conn.execute("DELETE FROM curriculum_plan WHERE brief_id IS NOT NULL").rowcount

    if table_exists(conn, "curriculum_artifact_template_proposal"):
        stats["curriculum_artifact_template_proposal"] = conn.execute(
            "DELETE FROM curriculum_artifact_template_proposal WHERE brief_id IS NOT NULL"
        ).rowcount

    if table_exists(conn, "skill_suggestion"):
        stats["skill_suggestion"] = conn.execute("DELETE FROM skill_suggestion WHERE brief_id IS NOT NULL").rowcount

    if table_exists(conn, "skill_promotion_log"):
        stats["skill_promotion_log"] = conn.execute("DELETE FROM skill_promotion_log").rowcount

    if table_exists(conn, "evidence_source"):
        stats["evidence_source"] = conn.execute("DELETE FROM evidence_source WHERE brief_id IS NOT NULL").rowcount

    if table_exists(conn, "evidence_query_cache"):
        stats["evidence_query_cache"] = conn.execute("DELETE FROM evidence_query_cache").rowcount

    if table_exists(conn, "intake_job"):
        stats["intake_job"] = conn.execute("DELETE FROM intake_job").rowcount

    if table_exists(conn, "profile_brief"):
        stats["profile_brief"] = conn.execute("DELETE FROM profile_brief").rowcount

    ACTIVE_INTAKE_JOB_IDS.clear()
    prune_stats = prune_empty_generated_catalog_nodes(conn)
    stats.update({f"prune_{key}": value for key, value in prune_stats.items()})
    conn.commit()
    return {key: int(value or 0) for key, value in stats.items()}


def list_catalog_groups(conn: sqlite3.Connection) -> list[dict[str, object]]:
    return fetch_all(
        conn,
        """
        SELECT
            sg.id,
            sg.name,
            sg.code,
            sg.sort_order,
            sg.status,
            sg.source,
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
          AND (
              COALESCE(sg.source, '') = 'manual'
              OR EXISTS (
                  SELECT 1
                  FROM skill s_visible
                  WHERE s_visible.group_id = sg.id
                    AND COALESCE(s_visible.is_active, 1) = 1
              )
          )
        GROUP BY sg.id, sg.name, sg.code, sg.sort_order, sg.status, sg.source
        ORDER BY sg.sort_order, sg.name
        """,
    )


def list_skill_sets(conn: sqlite3.Connection) -> list[dict[str, object]]:
    if not table_exists(conn, "skill_set"):
        return []
    return fetch_all(
        conn,
        """
        SELECT
            ss.id,
            ss.code,
            ss.title,
            ss.description,
            ss.source_type,
            ss.source_id,
            ss.source_ref,
            ss.status,
            ss.metadata_json,
            ss.created_at,
            ss.updated_at,
            COUNT(DISTINCT ssi.skill_id) AS skill_count,
            COUNT(DISTINCT CASE WHEN ssi.role = 'target' THEN ssi.skill_id END) AS target_count,
            COUNT(DISTINCT CASE WHEN ssi.role = 'prerequisite' THEN ssi.skill_id END) AS prerequisite_count,
            COUNT(DISTINCT CASE WHEN ssi.role = 'reinforcement' THEN ssi.skill_id END) AS reinforcement_count,
            COUNT(DISTINCT CASE WHEN ssi.role = 'assessment' THEN ssi.skill_id END) AS assessment_count
        FROM skill_set ss
        LEFT JOIN skill_set_item ssi ON ssi.skill_set_id = ss.id
        WHERE ss.status != 'archived'
        GROUP BY ss.id, ss.code, ss.title, ss.description, ss.source_type, ss.source_id,
                 ss.source_ref, ss.status, ss.metadata_json, ss.created_at, ss.updated_at
        ORDER BY ss.updated_at DESC, ss.created_at DESC, ss.id DESC
        """,
    )


def get_skill_set(conn: sqlite3.Connection, skill_set_id: int) -> dict[str, object] | None:
    if not table_exists(conn, "skill_set"):
        return None
    return fetch_one(
        conn,
        """
        SELECT
            ss.*,
            COUNT(DISTINCT ssi.skill_id) AS skill_count
        FROM skill_set ss
        LEFT JOIN skill_set_item ssi ON ssi.skill_set_id = ss.id
        WHERE ss.id = ?
        GROUP BY ss.id
        """,
        (skill_set_id,),
    )


def list_skill_set_items(conn: sqlite3.Connection, skill_set_id: int) -> list[dict[str, object]]:
    if not table_exists(conn, "skill_set_item"):
        return []
    return fetch_all(
        conn,
        """
        SELECT
            ssi.id,
            ssi.skill_id,
            ssi.suggestion_id,
            ssi.plan_row_id,
            ssi.role,
            ssi.weight,
            ssi.sort_order,
            ssi.rationale,
            s.canonical_name,
            COALESCE(sg.name, '') AS group_name
        FROM skill_set_item ssi
        JOIN skill s ON s.id = ssi.skill_id
        LEFT JOIN skill_group sg ON sg.id = s.group_id
        WHERE ssi.skill_set_id = ?
        ORDER BY ssi.sort_order, ssi.id
        """,
        (skill_set_id,),
    )


def get_catalog_group(conn: sqlite3.Connection, group_id: int) -> dict[str, object] | None:
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


def list_catalog_group_skills(conn: sqlite3.Connection, group_id: int) -> list[dict[str, object]]:
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


def get_catalog_skill(conn: sqlite3.Connection, skill_id: int) -> dict[str, object] | None:
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
            s.status,
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


def get_catalog_indicator(conn: sqlite3.Connection, indicator_id: int) -> dict[str, object] | None:
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


def list_catalog_indicators(conn: sqlite3.Connection, skill_id: int) -> list[dict[str, object]]:
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


def list_skill_aliases(conn: sqlite3.Connection, skill_id: int) -> list[dict[str, object]]:
    return fetch_all(
        conn,
        """
        SELECT id, alias, normalized_alias, source
        FROM skill_alias
        WHERE skill_id = ?
        ORDER BY source, alias, id
        """,
        (skill_id,),
    )


def find_alias_owner(conn: sqlite3.Connection, normalized_alias: str, exclude_skill_id: int | None = None) -> dict[str, object] | None:
    params: list[object] = [normalized_alias]
    exclude_clause = ""
    if exclude_skill_id is not None:
        exclude_clause = "AND s.id <> ?"
        params.append(exclude_skill_id)
    return fetch_one(
        conn,
        f"""
        SELECT s.id, s.name, s.canonical_name, s.is_active, s.status
        FROM skill_alias sa
        JOIN skill s ON s.id = sa.skill_id
        WHERE sa.normalized_alias = ?
          {exclude_clause}
          AND COALESCE(s.is_active, 1) = 1
          AND COALESCE(s.status, 'active') = 'active'
        ORDER BY s.id
        LIMIT 1
        """,
        tuple(params),
    )


def add_skill_alias(conn: sqlite3.Connection, skill_id: int, alias: str, source: str = "manual") -> str:
    cleaned = alias.strip()
    normalized_alias = normalize_catalog_key(cleaned)
    if not cleaned or not normalized_alias:
        return "empty"
    skill = get_catalog_skill(conn, skill_id)
    if not skill:
        return "missing_skill"
    conflict = find_alias_owner(conn, normalized_alias, exclude_skill_id=skill_id)
    if conflict:
        return "conflict"
    conn.execute(
        """
        INSERT OR IGNORE INTO skill_alias(skill_id, alias, normalized_alias, source)
        VALUES (?, ?, ?, ?)
        """,
        (skill_id, cleaned, normalized_alias, source),
    )
    conn.commit()
    return "added"


def remove_skill_alias(conn: sqlite3.Connection, skill_id: int, alias_id: int) -> str:
    row = fetch_one(
        conn,
        "SELECT id FROM skill_alias WHERE id = ? AND skill_id = ?",
        (alias_id, skill_id),
    )
    if not row:
        return "missing"
    conn.execute("DELETE FROM skill_alias WHERE id = ? AND skill_id = ?", (alias_id, skill_id))
    conn.commit()
    return "removed"


def search_catalog_skills(
    conn: sqlite3.Connection,
    query: str,
    exclude_skill_id: int | None = None,
    limit: int = 15,
) -> list[dict[str, object]]:
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return []
    params: list[object] = [normalized_query, normalized_query, normalized_query, normalized_query]
    exclude_clause = ""
    if exclude_skill_id is not None:
        exclude_clause = "AND s.id <> ?"
        params.append(exclude_skill_id)
    params.append(limit)
    return fetch_all(
        conn,
        f"""
        SELECT
            s.id,
            s.name,
            s.canonical_name,
            s.normalized_name,
            s.group_id,
            sg.name AS group_name,
            s.is_active,
            s.status,
            COUNT(DISTINCT i.id) AS indicator_count,
            COUNT(DISTINCT sa.id) AS alias_count
        FROM skill s
        LEFT JOIN skill_group sg ON sg.id = s.group_id
        LEFT JOIN indicator i ON i.skill_id = s.id AND i.is_active = 1
        LEFT JOIN skill_alias sa ON sa.skill_id = s.id
        WHERE (
            instr(search_norm(COALESCE(s.name, '')), ?) > 0
            OR instr(search_norm(COALESCE(s.canonical_name, '')), ?) > 0
            OR instr(search_norm(COALESCE(s.normalized_name, '')), ?) > 0
            OR EXISTS (
                SELECT 1
                FROM skill_alias sa2
                WHERE sa2.skill_id = s.id
                  AND instr(search_norm(sa2.alias), ?) > 0
            )
        )
        {exclude_clause}
        GROUP BY s.id, s.name, s.canonical_name, s.normalized_name, s.group_id, sg.name, s.is_active, s.status
        ORDER BY COALESCE(s.is_active, 1) DESC, s.name
        LIMIT ?
        """,
        tuple(params),
    )


def merge_catalog_skills(conn: sqlite3.Connection, source_skill_id: int, target_skill_id: int) -> dict[str, int | str]:
    if source_skill_id == target_skill_id:
        return {"status": "same_skill"}
    source = get_catalog_skill(conn, source_skill_id)
    target = get_catalog_skill(conn, target_skill_id)
    if not source or not target:
        return {"status": "missing_skill"}

    moved_aliases = 0
    moved_indicators = 0
    archived_duplicate_indicators = 0
    now = datetime.now(UTC).isoformat()

    # Preserve the source canonical label as an alias of the merge target.
    for alias in [source.get("name"), source.get("canonical_name"), source.get("source_skill_name")]:
        if alias and add_skill_alias(conn, target_skill_id, str(alias), source="merge") == "added":
            moved_aliases += 1

    for alias_row in list_skill_aliases(conn, source_skill_id):
        alias = str(alias_row["alias"] or "").strip()
        normalized_alias = str(alias_row["normalized_alias"] or "").strip() or normalize_catalog_key(alias)
        if not alias or not normalized_alias:
            continue
        conflict = find_alias_owner(conn, normalized_alias, exclude_skill_id=source_skill_id)
        if conflict and int(conflict["id"]) != target_skill_id:
            continue
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO skill_alias(skill_id, alias, normalized_alias, source)
            VALUES (?, ?, ?, ?)
            """,
            (target_skill_id, alias, normalized_alias, str(alias_row["source"] or "merge")),
        ).rowcount
        moved_aliases += max(int(inserted or 0), 0)

    for indicator in fetch_all(conn, "SELECT * FROM indicator WHERE skill_id = ? ORDER BY sort_order, id", (source_skill_id,)):
        duplicate = fetch_one(
            conn,
            """
            SELECT id
            FROM indicator
            WHERE skill_id = ?
              AND indicator_type = ?
              AND normalized_text = ?
            """,
            (target_skill_id, indicator["indicator_type"], indicator["normalized_text"]),
        )
        if duplicate:
            conn.execute(
                """
                UPDATE indicator
                SET is_active = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, indicator["id"]),
            )
            archived_duplicate_indicators += 1
            continue
        conn.execute(
            """
            UPDATE indicator
            SET skill_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (target_skill_id, now, indicator["id"]),
        )
        moved_indicators += 1

    if table_exists(conn, "skill_suggestion"):
        conn.execute(
            """
            UPDATE skill_suggestion
            SET canonical_skill_id = ?,
                resolution = 'alias'
            WHERE canonical_skill_id = ?
            """,
            (target_skill_id, source_skill_id),
        )
    if table_exists(conn, "skill_promotion_log"):
        conn.execute("UPDATE skill_promotion_log SET skill_id = ? WHERE skill_id = ?", (target_skill_id, source_skill_id))
    if table_exists(conn, "skill_prerequisite"):
        conn.execute("UPDATE skill_prerequisite SET src_skill_id = ? WHERE src_skill_id = ?", (target_skill_id, source_skill_id))
        conn.execute("UPDATE skill_prerequisite SET dst_skill_id = ? WHERE dst_skill_id = ?", (target_skill_id, source_skill_id))
    if table_exists(conn, "competency_skill"):
        conn.execute("UPDATE competency_skill SET skill_id = ? WHERE skill_id = ?", (target_skill_id, source_skill_id))

    conn.execute(
        """
        UPDATE skill
        SET is_active = 0,
            status = 'deprecated',
            match_note = COALESCE(match_note || char(10), '') || ?,
            updated_at = ?
        WHERE id = ?
        """,
        (f"Merged into skill #{target_skill_id}: {target.get('name') or target.get('canonical_name')}", now, source_skill_id),
    )
    refresh_catalog_skill_complexity(conn, target_skill_id, commit=False)
    refresh_catalog_skill_complexity(conn, source_skill_id, commit=False)
    conn.commit()
    return {
        "status": "merged",
        "moved_aliases": moved_aliases,
        "moved_indicators": moved_indicators,
        "archived_duplicate_indicators": archived_duplicate_indicators,
    }


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


def create_catalog_group(conn: sqlite3.Connection, name: str, sort_order: int, status: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO skill_group (code, name, sort_order, status, source, updated_at)
        VALUES (?, ?, ?, ?, 'manual', ?)
        """,
        (f"group-{slugify(name)}", name.strip(), sort_order, status, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_catalog_group(conn: sqlite3.Connection, group_id: int, name: str, sort_order: int, status: str) -> None:
    conn.execute(
        """
        UPDATE skill_group
        SET code = ?, name = ?, sort_order = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (f"group-{slugify(name)}", name.strip(), sort_order, status, datetime.now(UTC).isoformat(), group_id),
    )
    conn.commit()


def remove_catalog_group(conn: sqlite3.Connection, group_id: int) -> str:
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


def restore_catalog_group(conn: sqlite3.Connection, group_id: int) -> str:
    group = get_catalog_group(conn, group_id)
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


def create_catalog_skill(
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
            canonical_name,
            name,
            normalized_name,
            skill_type,
            status,
            sort_order,
            description,
            source_skill_name,
            resolution_status,
            match_note,
            is_active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_id,
            f"skill-{slugify(name)}-{group_id}",
            name.strip(),
            name.strip(),
            normalize_catalog_key(name),
            "active" if is_active else "candidate",
            sort_order,
            description.strip() or None,
            source_skill_name.strip() or None,
            resolution_status,
            match_note.strip() or None,
            is_active,
            datetime.now(UTC).isoformat(),
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_catalog_skill(
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
    skill = get_catalog_skill(conn, skill_id)
    if not skill:
        return
    conn.execute(
        """
        UPDATE skill
        SET code = ?,
            canonical_name = ?,
            name = ?,
            normalized_name = ?,
            status = ?,
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
            name.strip(),
            normalize_catalog_key(name),
            "active" if is_active else "candidate",
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


def remove_catalog_skill(conn: sqlite3.Connection, skill_id: int) -> str:
    skill = get_catalog_skill(conn, skill_id)
    if not skill:
        return "missing"

    indicator_count = conn.execute("SELECT COUNT(*) FROM indicator WHERE skill_id = ?", (skill_id,)).fetchone()[0]
    if indicator_count:
        conn.execute(
            """
            UPDATE skill
            SET is_active = 0,
                status = 'candidate',
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


def restore_catalog_skill(conn: sqlite3.Connection, skill_id: int) -> str:
    skill = get_catalog_skill(conn, skill_id)
    if not skill:
        return "missing"
    conn.execute(
        """
        UPDATE skill
        SET is_active = 1,
            status = 'active',
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
    refresh_catalog_skill_complexity(conn, skill_id, commit=False)
    conn.commit()
    return "restored"


def create_catalog_indicator(
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
    refresh_catalog_skill_complexity(conn, skill_id, commit=False)
    conn.commit()
    return int(cursor.lastrowid)


def update_catalog_indicator(
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
    refresh_catalog_skill_complexity(conn, row["skill_id"], commit=False)
    conn.commit()


def remove_catalog_indicator(conn: sqlite3.Connection, indicator_id: int) -> str:
    indicator = get_catalog_indicator(conn, indicator_id)
    if not indicator:
        return "missing"

    skill_id = int(indicator["skill_id"])
    if indicator.get("source_profile_name") == "manual":
        conn.execute("DELETE FROM indicator WHERE id = ?", (indicator_id,))
        refresh_catalog_skill_complexity(conn, skill_id, commit=False)
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
    refresh_catalog_skill_complexity(conn, skill_id, commit=False)
    conn.commit()
    return "archived"


def restore_catalog_indicator(conn: sqlite3.Connection, indicator_id: int) -> str:
    indicator = get_catalog_indicator(conn, indicator_id)
    if not indicator:
        return "missing"

    skill = get_catalog_skill(conn, int(indicator["skill_id"]))
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
        refresh_catalog_skill_complexity(conn, skill["id"], commit=False)
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

    existing_group_names = {normalize_competency_title(group["name"]) for group in groups}
    groups.extend(list_canonical_directory_additions(conn, query, scope, existing_group_names))
    return groups, profile


def list_canonical_directory_additions(
    conn: sqlite3.Connection,
    query: str,
    scope: str,
    existing_group_names: set[str],
) -> list[dict[str, object]]:
    """Return accepted canonical competencies that are not part of the imported live hierarchy yet."""
    from spravochnik_intake.pipeline import competency_catalog

    required_tables = ("profile", "profile_competency", "competency", "competency_skill", "skill")
    if not all(table_exists(conn, name) for name in required_tables):
        return []

    profile_competencies = fetch_all(
        conn,
        """
        SELECT
            pc.id AS profile_competency_id,
            c.id AS competency_id,
            c.title,
            c.status,
            COUNT(DISTINCT cs.skill_id) AS skill_count
        FROM profile p
        JOIN profile_competency pc ON pc.profile_id = p.id
        JOIN competency c ON c.id = pc.competency_id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        WHERE p.slug = ?
          AND pc.review_state = 'accepted'
          AND c.status = 'active'
        GROUP BY pc.id, c.id, c.title, c.status
        HAVING COUNT(DISTINCT cs.skill_id) > 0
        ORDER BY c.title
        """,
        (competency_catalog.SERVICE_PROFILE_SLUG,),
    )
    profile_competencies = [
        row for row in profile_competencies if normalize_competency_title(row["title"]) not in existing_group_names
    ]
    if not profile_competencies:
        return []

    pc_ids = [int(row["profile_competency_id"]) for row in profile_competencies]
    placeholders = ", ".join("?" for _ in pc_ids)
    skill_rows = fetch_all(
        conn,
        f"""
        SELECT
            cs.id,
            cs.profile_competency_id,
            cs.source_skill_name,
            cs.skill_order,
            s.id AS skill_id,
            s.canonical_name,
            COALESCE(s.resolution_status, 'matched') AS resolution_status,
            s.match_note
        FROM competency_skill cs
        JOIN skill s ON s.id = cs.skill_id
        WHERE cs.profile_competency_id IN ({placeholders})
        ORDER BY cs.profile_competency_id, cs.skill_order, s.canonical_name
        """,
        tuple(pc_ids),
    )
    indicator_rows = fetch_all(
        conn,
        f"""
        SELECT
            cs.id AS competency_skill_id,
            s.id AS skill_id,
            COALESCE(d.title, 'Не указано') AS dimension_title,
            COALESCE(NULLIF(TRIM(ilc.raw_value), ''), NULLIF(TRIM(ir.base_text), '')) AS raw_value,
            COALESCE(ilc.sort_order, 0) AS sort_order
        FROM competency_skill cs
        JOIN skill s ON s.id = cs.skill_id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        LEFT JOIN dimension d ON d.id = ir.dimension_id
        LEFT JOIN indicator_level_cell ilc ON ilc.indicator_row_id = ir.id
        WHERE cs.profile_competency_id IN ({placeholders})
          AND COALESCE(NULLIF(TRIM(ilc.raw_value), ''), NULLIF(TRIM(ir.base_text), '')) IS NOT NULL
        ORDER BY
            s.canonical_name,
            CASE COALESCE(d.title, '')
                WHEN 'Знает' THEN 1
                WHEN 'Умеет' THEN 2
                ELSE 3
            END,
            COALESCE(ilc.sort_order, 0),
            raw_value
        """,
        tuple(pc_ids),
    )

    indicator_map: dict[int, list[dict[str, object]]] = {}
    seen_by_skill: dict[int, set[str]] = {}
    for row in indicator_rows:
        skill_id = int(row["skill_id"])
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

    resolution_labels = {
        "matched": "совпало",
        "alias": "сопоставлено по alias",
        "manual": "сопоставлено вручную",
        "fuzzy": "сопоставлено нечетко",
        "missing": "нет локального skill",
    }
    skills_by_pc: dict[int, list[dict[str, object]]] = {}
    for row in skill_rows:
        display_name = row["canonical_name"] or row["source_skill_name"]
        indicators = indicator_map.get(int(row["skill_id"]), [])
        skills_by_pc.setdefault(int(row["profile_competency_id"]), []).append(
            {
                "id": row["id"],
                "display_name": display_name,
                "source_name": row["source_skill_name"] or display_name,
                "resolved_name": row["canonical_name"],
                "resolution_status": row["resolution_status"],
                "resolution_label": resolution_labels.get(row["resolution_status"], row["resolution_status"]),
                "match_note": row["match_note"],
                "indicator_count": len(indicators),
                "indicators": indicators,
            }
        )

    query_folded = query.casefold()
    groups: list[dict[str, object]] = []
    for row in profile_competencies:
        group_name = display_catalog_title(row["title"])
        group_matches = bool(query_folded) and query_folded in group_name.casefold()
        all_skills = skills_by_pc.get(int(row["profile_competency_id"]), [])
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
                "id": f"canonical-{row['competency_id']}",
                "name": group_name,
                "skill_count": len(matched_skills),
                "indicator_count": sum(skill["indicator_count"] for skill in matched_skills),
                "skills": matched_skills,
                "open_on_load": bool(query_folded),
            }
        )
    return groups


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
        HAVING COUNT(DISTINCT cs.skill_id) > 0
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


def list_profiles(conn: sqlite3.Connection, include_service: bool = False) -> list[dict[str, object]]:
    where_clause = "" if include_service else "WHERE p.slug != 'intake-accepted-skills'"
    return fetch_all(
        conn,
        f"""
        SELECT
            p.id,
            p.name,
            p.slug,
            p.source_kind,
            COUNT(DISTINCT pc.id) AS competency_count,
            COUNT(DISTINCT cs.id) AS skill_count,
            COUNT(DISTINCT ir.id) AS indicator_count,
            COUNT(DISTINCT CASE WHEN pc.review_state = 'needs_review' THEN pc.id END) AS review_competencies
        FROM profile p
        LEFT JOIN profile_competency pc ON pc.profile_id = p.id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        LEFT JOIN indicator_row ir ON ir.competency_skill_id = cs.id
        {where_clause}
        GROUP BY p.id, p.name, p.slug, p.source_kind
        ORDER BY p.name
        """,
    )


def list_candidate_competencies(conn: sqlite3.Connection) -> list[dict[str, object]]:
    from spravochnik_intake.pipeline import competency_catalog

    if not all(table_exists(conn, name) for name in ("profile", "profile_competency", "competency")):
        return []
    rows = fetch_all(
        conn,
        """
        SELECT
            pc.id AS profile_competency_id,
            pc.review_state,
            c.id AS competency_id,
            c.title,
            c.status,
            rq.id AS review_id,
            rq.reason_code,
            rq.details,
            rq.created_at,
            COUNT(DISTINCT cs.skill_id) AS skill_count,
            GROUP_CONCAT(DISTINCT s.canonical_name) AS skill_names
        FROM profile p
        JOIN profile_competency pc ON pc.profile_id = p.id
        JOIN competency c ON c.id = pc.competency_id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        LEFT JOIN skill s ON s.id = cs.skill_id
        LEFT JOIN review_queue rq
          ON rq.entity_type = 'competency'
         AND rq.entity_id = c.id
         AND rq.status = 'open'
        WHERE p.slug = ?
          AND (
              pc.review_state = 'needs_review'
              OR c.status = 'candidate'
              OR rq.id IS NOT NULL
          )
        GROUP BY pc.id, pc.review_state, c.id, c.title, c.status, rq.id, rq.reason_code, rq.details, rq.created_at
        ORDER BY rq.created_at DESC, pc.id DESC
        """,
        (competency_catalog.SERVICE_PROFILE_SLUG,),
    )
    for row in rows:
        row["skills"] = list_candidate_competency_skills(conn, int(row["profile_competency_id"]))
        similar = list_competency_similarity_candidates(
            conn,
            int(row["competency_id"]),
            int(row["profile_competency_id"]),
        )
        row["similar_competencies"] = similar
        row["nearest_competency"] = similar[0] if similar else None
    return rows


def list_candidate_competency_skills(conn: sqlite3.Connection, profile_competency_id: int) -> list[dict[str, object]]:
    if not all(table_exists(conn, name) for name in ("competency_skill", "skill")):
        return []
    return fetch_all(
        conn,
        """
        SELECT
            cs.id AS competency_skill_id,
            cs.skill_id,
            cs.source_skill_name,
            cs.review_state,
            s.canonical_name,
            s.status AS skill_status
        FROM competency_skill cs
        LEFT JOIN skill s ON s.id = cs.skill_id
        WHERE cs.profile_competency_id = ?
        ORDER BY cs.skill_order, cs.id
        """,
        (profile_competency_id,),
    )


def competency_token_set(value: object | None) -> set[str]:
    stopwords = {
        "и",
        "в",
        "во",
        "на",
        "для",
        "по",
        "с",
        "со",
        "к",
        "от",
        "до",
        "при",
        "или",
        "а",
        "the",
        "and",
        "of",
        "for",
    }
    return {token for token in normalize_competency_title(value).split() if len(token) > 2 and token not in stopwords}


def _competency_similarity_label(score: float) -> tuple[str, str]:
    if score >= 82:
        return "Высокая похожесть", "merge"
    if score >= 62:
        return "Средняя похожесть", "review"
    return "Слабая похожесть", "create"


def list_competency_similarity_candidates(
    conn: sqlite3.Connection,
    competency_id: int,
    profile_competency_id: int,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Find existing competencies that may duplicate a candidate grouping."""
    if not all(table_exists(conn, name) for name in ("competency", "profile_competency", "competency_skill")):
        return []
    candidate = conn.execute(
        """
        SELECT c.id, c.title
        FROM competency c
        JOIN profile_competency pc ON pc.competency_id = c.id
        WHERE c.id = ? AND pc.id = ?
        """,
        (competency_id, profile_competency_id),
    ).fetchone()
    if not candidate:
        return []
    candidate_skill_ids = {
        int(row["skill_id"])
        for row in conn.execute(
            "SELECT skill_id FROM competency_skill WHERE profile_competency_id = ? AND skill_id IS NOT NULL",
            (profile_competency_id,),
        )
    }
    candidate_tokens = competency_token_set(candidate["title"])
    rows = fetch_all(
        conn,
        """
        SELECT
            c.id,
            c.title,
            c.status,
            COUNT(DISTINCT pc.profile_id) AS profile_count,
            COUNT(DISTINCT cs.skill_id) AS skill_count,
            GROUP_CONCAT(DISTINCT cs.skill_id) AS skill_ids
        FROM competency c
        LEFT JOIN profile_competency pc ON pc.competency_id = c.id
        LEFT JOIN competency_skill cs ON cs.profile_competency_id = pc.id
        WHERE c.status = 'active'
          AND c.id != ?
        GROUP BY c.id, c.title, c.status
        HAVING COUNT(DISTINCT cs.skill_id) > 0
        """,
        (competency_id,),
    )
    scored: list[dict[str, object]] = []
    for row in rows:
        target_tokens = competency_token_set(row["title"])
        token_union = candidate_tokens | target_tokens
        token_overlap = len(candidate_tokens & target_tokens) / len(token_union) if token_union else 0.0
        title_ratio = SequenceMatcher(
            None,
            normalize_competency_title(candidate["title"]),
            normalize_competency_title(row["title"]),
        ).ratio()
        target_skill_ids = {
            int(value)
            for value in str(row.get("skill_ids") or "").split(",")
            if value and str(value).isdigit()
        }
        skill_overlap_count = len(candidate_skill_ids & target_skill_ids)
        skill_overlap = skill_overlap_count / max(1, len(candidate_skill_ids)) if candidate_skill_ids else 0.0
        score = round((0.45 * title_ratio + 0.35 * token_overlap + 0.20 * skill_overlap) * 100, 2)
        if score < 28 and skill_overlap_count == 0:
            continue
        label, recommendation = _competency_similarity_label(score)
        row.update(
            {
                "score": score,
                "label": label,
                "recommendation": recommendation,
                "token_overlap_pct": round(token_overlap * 100, 2),
                "title_similarity_pct": round(title_ratio * 100, 2),
                "skill_overlap_count": skill_overlap_count,
                "candidate_skill_count": len(candidate_skill_ids),
            }
        )
        scored.append(row)
    scored.sort(key=lambda item: (float(item["score"]), int(item["skill_overlap_count"])), reverse=True)
    return scored[:limit]


def list_active_competency_options(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, object]]:
    if not table_exists(conn, "competency"):
        return []
    return fetch_all(
        conn,
        """
        SELECT id, title, status
        FROM competency
        WHERE status = 'active'
        ORDER BY title
        LIMIT ?
        """,
        (limit,),
    )


def normalize_competency_title(value: object | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я+]+", " ", text)
    return " ".join(text.split())


def display_catalog_title(value: object | None) -> str:
    text = str(value or "").strip()
    return text[:1].upper() + text[1:] if text else ""


def rename_candidate_competency(conn: sqlite3.Connection, competency_id: int, new_title: str) -> dict[str, object]:
    title = " ".join(str(new_title or "").split())
    if not title:
        return {"status": "empty_title", "competency_id": competency_id}
    normalized_title = normalize_competency_title(title)
    existing = conn.execute(
        "SELECT id FROM competency WHERE normalized_title = ? AND id != ?",
        (normalized_title, competency_id),
    ).fetchone()
    if existing:
        return {"status": "conflict", "competency_id": competency_id, "target_competency_id": int(existing["id"])}
    conn.execute(
        "UPDATE competency SET title = ?, normalized_title = ? WHERE id = ?",
        (title, normalized_title, competency_id),
    )
    conn.execute(
        "UPDATE profile_competency SET title_in_source = COALESCE(NULLIF(title_in_source, ''), ?) WHERE competency_id = ?",
        (title, competency_id),
    )
    if table_exists(conn, "source_block"):
        conn.execute(
            """
            UPDATE source_block
            SET raw_title = ?
            WHERE id IN (
                SELECT source_block_id FROM profile_competency WHERE competency_id = ?
            )
            """,
            (title, competency_id),
        )
    conn.commit()
    return {"status": "renamed", "competency_id": competency_id, "title": title}


def ensure_service_profile_competency(conn: sqlite3.Connection, target_competency_id: int) -> int | None:
    from spravochnik_intake.pipeline import competency_catalog

    context = competency_catalog.ensure_catalog_context(conn)
    if context is None:
        return None
    existing = conn.execute(
        """
        SELECT id
        FROM profile_competency
        WHERE profile_id = ? AND competency_id = ?
        ORDER BY id LIMIT 1
        """,
        (context.profile_id, target_competency_id),
    ).fetchone()
    if existing:
        return int(existing["id"])
    target = conn.execute("SELECT title FROM competency WHERE id = ?", (target_competency_id,)).fetchone()
    if not target:
        return None
    block_no = int(
        conn.execute(
            "SELECT COALESCE(MAX(block_no), 0) + 10 FROM source_block WHERE source_sheet_id = ?",
            (context.source_sheet_id,),
        ).fetchone()[0]
        or 10
    )
    cursor = conn.execute(
        """
        INSERT INTO source_block(
            source_sheet_id, block_no, header_row_number, level_row_number,
            end_row_number, raw_title, raw_description, raw_prerequisites, raw_scale_signature
        )
        VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, NULL)
        """,
        (context.source_sheet_id, block_no, block_no, target["title"], "Служебный блок intake для переноса skill."),
    )
    source_block_id = int(cursor.lastrowid)
    sort_order = int(
        conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM profile_competency WHERE profile_id = ?",
            (context.profile_id,),
        ).fetchone()[0]
        or 10
    )
    cursor = conn.execute(
        """
        INSERT INTO profile_competency(
            profile_id, competency_id, source_block_id, scale_id, title_in_source,
            description_in_source, prerequisites_text, sort_order, review_state
        )
        VALUES (?, ?, ?, NULL, ?, NULL, NULL, ?, 'accepted')
        """,
        (context.profile_id, target_competency_id, source_block_id, target["title"], sort_order),
    )
    return int(cursor.lastrowid)


def move_candidate_competency_skill(
    conn: sqlite3.Connection,
    competency_skill_id: int,
    target_competency_id: int,
) -> dict[str, object]:
    target_profile_competency_id = ensure_service_profile_competency(conn, target_competency_id)
    if target_profile_competency_id is None:
        return {"status": "target_missing", "competency_skill_id": competency_skill_id}
    row = conn.execute(
        """
        SELECT
            cs.id,
            cs.profile_competency_id,
            cs.skill_id,
            cs.source_skill_name,
            pc.competency_id AS source_competency_id
        FROM competency_skill cs
        JOIN profile_competency pc ON pc.id = cs.profile_competency_id
        WHERE cs.id = ?
        """,
        (competency_skill_id,),
    ).fetchone()
    if not row:
        return {"status": "missing", "competency_skill_id": competency_skill_id}
    source_competency_id = int(row["source_competency_id"])
    existing = conn.execute(
        """
        SELECT id
        FROM competency_skill
        WHERE profile_competency_id = ? AND skill_id = ?
        """,
        (target_profile_competency_id, row["skill_id"]),
    ).fetchone()
    if existing:
        target_competency_skill_id = int(existing["id"])
        conn.execute(
            "UPDATE indicator_row SET competency_skill_id = ? WHERE competency_skill_id = ?",
            (target_competency_skill_id, competency_skill_id),
        )
        conn.execute("DELETE FROM competency_skill WHERE id = ?", (competency_skill_id,))
    else:
        next_order = int(
            conn.execute(
                "SELECT COALESCE(MAX(skill_order), 0) + 10 FROM competency_skill WHERE profile_competency_id = ?",
                (target_profile_competency_id,),
            ).fetchone()[0]
            or 10
        )
        conn.execute(
            """
            UPDATE competency_skill
            SET profile_competency_id = ?,
                skill_order = ?,
                review_state = 'accepted'
            WHERE id = ?
            """,
            (target_profile_competency_id, next_order, competency_skill_id),
        )
    prune_empty_profile_competencies(conn)
    close_candidate_competency_if_empty(
        conn,
        source_competency_id,
        f"Все skills перенесены в существующую competency #{target_competency_id}.",
    )
    conn.commit()
    return {
        "status": "moved",
        "competency_skill_id": competency_skill_id,
        "target_competency_id": target_competency_id,
    }


def merge_candidate_competency(
    conn: sqlite3.Connection,
    competency_id: int,
    target_competency_id: int,
) -> dict[str, object]:
    if competency_id == target_competency_id:
        return {"status": "same_competency", "competency_id": competency_id}
    profile_rows = conn.execute(
        "SELECT id FROM profile_competency WHERE competency_id = ?",
        (competency_id,),
    ).fetchall()
    moved = 0
    for profile_row in profile_rows:
        for skill_row in conn.execute(
            "SELECT id FROM competency_skill WHERE profile_competency_id = ?",
            (int(profile_row["id"]),),
        ).fetchall():
            result = move_candidate_competency_skill(conn, int(skill_row["id"]), target_competency_id)
            if result.get("status") == "moved":
                moved += 1
    conn.execute("UPDATE competency SET status = 'deprecated' WHERE id = ?", (competency_id,))
    resolve_candidate_competency(conn, competency_id, "reject", f"Слито с competency #{target_competency_id}.")
    return {"status": "merged", "competency_id": competency_id, "target_competency_id": target_competency_id, "moved": moved}


def prune_empty_profile_competencies(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "profile_competency"):
        return 0
    deleted = conn.execute(
        """
        DELETE FROM profile_competency
        WHERE NOT EXISTS (
            SELECT 1 FROM competency_skill cs WHERE cs.profile_competency_id = profile_competency.id
        )
        AND review_state != 'accepted'
        """
    ).rowcount
    return int(deleted or 0)


def close_candidate_competency_if_empty(conn: sqlite3.Connection, competency_id: int, resolution_note: str) -> bool:
    if not all(table_exists(conn, name) for name in ("competency", "profile_competency", "competency_skill")):
        return False
    remaining = conn.execute(
        """
        SELECT 1
        FROM competency_skill cs
        JOIN profile_competency pc ON pc.id = cs.profile_competency_id
        WHERE pc.competency_id = ?
        LIMIT 1
        """,
        (competency_id,),
    ).fetchone()
    if remaining:
        return False
    conn.execute(
        """
        UPDATE competency
        SET status = 'deprecated'
        WHERE id = ?
          AND status = 'candidate'
        """,
        (competency_id,),
    )
    if table_exists(conn, "review_queue"):
        now = utc_now_iso()
        conn.execute(
            """
            UPDATE review_queue
            SET status = 'ignored',
                resolution_note = COALESCE(NULLIF(?, ''), resolution_note),
                reviewed_at = ?,
                updated_at = ?
            WHERE entity_type = 'competency'
              AND entity_id = ?
              AND status = 'open'
              AND source_ref LIKE 'intake_accept:%'
            """,
            (resolution_note, now, now, competency_id),
        )
    return True


def count_open_candidate_competencies(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "review_queue"):
        return 0
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM review_queue
            WHERE entity_type = 'competency'
              AND status = 'open'
              AND source_ref LIKE 'intake_accept:%'
            """
        ).fetchone()[0]
    )


def resolve_candidate_competency(
    conn: sqlite3.Connection,
    competency_id: int,
    action: str,
    resolution_note: str = "",
) -> dict[str, object]:
    from spravochnik_intake.pipeline import competency_catalog

    if action == "accept":
        result = competency_catalog.resolve_competency_candidate(conn, competency_id=competency_id, accepted=True)
        review_status = "resolved"
    elif action == "reject":
        result = competency_catalog.resolve_competency_candidate(conn, competency_id=competency_id, accepted=False)
        review_status = "ignored"
    elif action == "review":
        result = competency_catalog.reopen_competency_candidate(conn, competency_id=competency_id)
        review_status = "open"
    else:
        return {"status": "invalid_action", "competency_id": competency_id}

    now = utc_now_iso()
    reviewed_at = None if review_status == "open" else now
    if table_exists(conn, "review_queue"):
        conn.execute(
            """
            UPDATE review_queue
            SET status = ?,
                resolution_note = COALESCE(?, resolution_note),
                reviewed_at = ?,
                updated_at = ?
            WHERE entity_type = 'competency'
              AND entity_id = ?
              AND source_ref LIKE 'intake_accept:%'
            """,
            (review_status, resolution_note.strip() or None, reviewed_at, now, competency_id),
        )
    conn.commit()
    return result


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
    entity_type_filter: str = "all",
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, str]], list[dict[str, str]]]:
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
    if entity_type_filter != "all":
        where_parts.append("entity_type = ?")
        params.append(entity_type_filter)
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
        format_prerequisite_edge_review(item)

    reason_options = [
        {"code": row["reason_code"], "label": review_reason_label(str(row["reason_code"]))}
        for row in conn.execute("SELECT DISTINCT reason_code FROM review_queue ORDER BY reason_code")
    ]
    entity_type_options = [
        {"code": row["entity_type"], "label": review_entity_label(str(row["entity_type"]))}
        for row in conn.execute("SELECT DISTINCT entity_type FROM review_queue ORDER BY entity_type")
    ]
    return status_totals, breakdown, items, reason_options, entity_type_options


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


def create_app(db_path: Path, summary_path: Path):
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["datetime_local"] = format_local_datetime
    env.filters["review_entity_label"] = review_entity_label
    env.filters["review_source_label"] = review_source_label
    env.filters["review_text_label"] = review_text_label
    env.filters["edge_reason_label"] = edge_reason_label
    env.filters["review_severity_label"] = review_severity_label
    env.filters["review_status_label"] = review_status_label
    startup_conn = open_db(db_path)
    try:
        ensure_intake_runtime_schema(startup_conn, db_path)
        repair_dirty_profile_names(startup_conn)
    finally:
        startup_conn.close()
    summary = refresh_summary_counts(load_summary(summary_path), db_path)

    def render(template_name: str, context: dict[str, object]) -> str:
        template = env.get_template(template_name)
        current_summary = refresh_summary_counts(summary, db_path)
        request_path = str(context.get("request_path", "/"))
        shared = {
            "nav": get_main_nav(),
            "secondary_nav": get_secondary_nav(request_path),
            "show_secondary_nav": show_secondary_nav(request_path),
            "route_zone": detect_route_zone(request_path),
            "complexity_options": COMPLEXITY_OPTIONS,
            "intake_progress_steps": INTAKE_PROGRESS_STEPS,
            "summary": current_summary,
            "request_path": request_path,
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
            return redirect_response(start_response, "/intake")

        if path == "/catalog-admin":
            return redirect_response(start_response, "/catalog-admin/groups")

        if path == "/catalog-admin/candidate-competencies" and method == "GET":
            catalog_conn = open_db(db_path)
            try:
                ensure_intake_runtime_schema(catalog_conn, db_path)
                candidates = list_candidate_competencies(catalog_conn)
                competency_options = list_active_competency_options(catalog_conn)
                html = render(
                    "catalog_admin_candidate_competencies.html",
                    {
                        "title": "Кандидатные компетенции",
                        "candidates": candidates,
                        "competency_options": competency_options,
                        "open_count": len([item for item in candidates if str(item.get("review_state") or "") == "needs_review"]),
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                catalog_conn.close()

        if path == "/catalog-admin/candidate-competencies" and method == "POST":
            catalog_conn = open_db(db_path)
            try:
                ensure_intake_runtime_schema(catalog_conn, db_path)
                form_data = parse_post_data(environ)
                competency_id = int(form_data.get("competency_id", "0") or 0)
                action = form_data.get("action", "")
                if not competency_id or action not in {"accept", "reject", "review", "rename", "merge", "move_skill"}:
                    return not_found(start_response, "Invalid candidate competency action")
                if action == "rename":
                    rename_candidate_competency(catalog_conn, competency_id, form_data.get("new_title", ""))
                elif action == "merge":
                    target_competency_id = int(form_data.get("target_competency_id", "0") or 0)
                    if not target_competency_id:
                        return not_found(start_response, "Target competency is required")
                    merge_candidate_competency(catalog_conn, competency_id, target_competency_id)
                elif action == "move_skill":
                    target_competency_id = int(form_data.get("target_competency_id", "0") or 0)
                    competency_skill_id = int(form_data.get("competency_skill_id", "0") or 0)
                    if not target_competency_id or not competency_skill_id:
                        return not_found(start_response, "Target competency and skill link are required")
                    move_candidate_competency_skill(catalog_conn, competency_skill_id, target_competency_id)
                else:
                    resolve_candidate_competency(
                        catalog_conn,
                        competency_id=competency_id,
                        action=action,
                        resolution_note=form_data.get("resolution_note", ""),
                    )
                return redirect_response(start_response, "/catalog-admin/candidate-competencies")
            finally:
                catalog_conn.close()

        if path == "/catalog-admin/archive" and method == "GET":
            catalog_conn = open_db(db_path)
            try:
                archive_query = query_params.get("q", [""])[-1].strip()
                archive_scope = query_params.get("scope", ["all"])[-1].strip() or "all"
                if archive_scope not in {"all", "groups", "skills", "indicators"}:
                    archive_scope = "all"

                groups = list_archived_groups(catalog_conn, archive_query) if archive_scope in {"all", "groups"} else []
                skills = list_archived_skills(catalog_conn, archive_query) if archive_scope in {"all", "skills"} else []
                indicators = list_archived_indicators(catalog_conn, archive_query) if archive_scope in {"all", "indicators"} else []
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
                catalog_conn.close()

        if path == "/catalog-admin/archive" and method == "POST":
            catalog_conn = open_db(db_path)
            try:
                form_data = parse_post_data(environ)
                action = form_data.get("action", "")
                if action == "restore_group":
                    restore_catalog_group(catalog_conn, int(form_data["group_id"]))
                elif action == "restore_skill":
                    restore_catalog_skill(catalog_conn, int(form_data["skill_id"]))
                elif action == "restore_indicator":
                    restore_catalog_indicator(catalog_conn, int(form_data["indicator_id"]))
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
                catalog_conn.close()

        if path == "/catalog-admin/artifact-templates" and method == "GET":
            from spravochnik_intake.pipeline import storage as intake_storage

            catalog_conn = open_db(db_path)
            try:
                ensure_intake_runtime_schema(catalog_conn, db_path)
                templates = intake_storage.load_curriculum_artifact_templates(catalog_conn, active_only=False)
                edit_id = parse_optional_int(query_params.get("edit", [""])[-1])
                edit_template = next((dict(item) for item in templates if int(item.get("id") or 0) == edit_id), None)
                if edit_template:
                    scopes = edit_template.get("scopes") if isinstance(edit_template.get("scopes"), list) else []
                    first_scope = scopes[0] if scopes else {}
                    edit_template["scope_type"] = str(first_scope.get("scope_type") or "coverage_area") if isinstance(first_scope, dict) else "coverage_area"
                    edit_template["scope_weight"] = str(first_scope.get("weight") or "1.0") if isinstance(first_scope, dict) else "1.0"
                    edit_template["scope_names_text"] = "\n".join(
                        str(scope.get("scope_name") or "")
                        for scope in scopes
                        if isinstance(scope, dict) and str(scope.get("scope_type") or "") != "any"
                    )
                html = render(
                    "catalog_admin_artifact_templates.html",
                    {
                        "title": "Шаблоны УП",
                        "templates": templates,
                        "edit_template": edit_template,
                        "artifact_family_options": ARTIFACT_FAMILY_OPTIONS,
                        "scope_type_options": ARTIFACT_SCOPE_TYPE_OPTIONS,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                catalog_conn.close()

        if path == "/catalog-admin/artifact-templates" and method == "POST":
            from spravochnik_intake.pipeline import storage as intake_storage

            catalog_conn = open_db(db_path)
            try:
                ensure_intake_runtime_schema(catalog_conn, db_path)
                form_data = parse_post_data(environ)
                action = form_data.get("action", "")
                template_id = parse_optional_int(form_data.get("template_id"))
                if action == "save_template":
                    priority = parse_optional_int(form_data.get("priority")) or 100
                    intake_storage.upsert_curriculum_artifact_template(
                        catalog_conn,
                        code=form_data.get("code", "").strip() or form_data.get("title", "").strip(),
                        title=form_data.get("title", "").strip() or "Шаблон артефакта",
                        artifact_family=form_data.get("artifact_family", "practice").strip() or "practice",
                        artifact_description=form_data.get("artifact_description", "").strip(),
                        project_name_pattern=form_data.get("project_name_pattern", "").strip(),
                        materials_pattern=form_data.get("materials_pattern", "").strip(),
                        storytelling_pattern=form_data.get("storytelling_pattern", "").strip(),
                        validation_criteria=form_data.get("validation_criteria", "").strip(),
                        priority=priority,
                        status=form_data.get("status", "active").strip() or "active",
                        source="methodologist",
                        scopes=parse_artifact_template_scopes(form_data),
                    )
                elif action in {"activate_template", "deprecate_template"} and template_id:
                    status = "active" if action == "activate_template" else "deprecated"
                    catalog_conn.execute(
                        """
                        UPDATE curriculum_artifact_template
                        SET status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (status, utc_now_iso(), template_id),
                    )
                    catalog_conn.commit()
                return redirect_response(start_response, "/catalog-admin/artifact-templates")
            finally:
                catalog_conn.close()

        if path == "/catalog-admin/skillsets" and method == "GET":
            catalog_conn = open_db(db_path)
            try:
                ensure_intake_runtime_schema(catalog_conn, db_path)
                skill_sets = list_skill_sets(catalog_conn)
                html = render(
                    "catalog_admin_skillsets.html",
                    {
                        "title": "Наборы skills",
                        "skill_sets": skill_sets,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                catalog_conn.close()

        if path.startswith("/catalog-admin/skillsets/") and method == "GET":
            skill_set_id = parse_path_int(path, "/catalog-admin/skillsets/")
            if skill_set_id is None:
                return not_found(start_response)
            catalog_conn = open_db(db_path)
            try:
                ensure_intake_runtime_schema(catalog_conn, db_path)
                skill_set = get_skill_set(catalog_conn, skill_set_id)
                if not skill_set:
                    return not_found(start_response, "Skill set not found")
                items = list_skill_set_items(catalog_conn, skill_set_id)
                html = render(
                    "catalog_admin_skillset_detail.html",
                    {
                        "title": str(skill_set["title"]),
                        "skill_set": skill_set,
                        "items": items,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                catalog_conn.close()

        if path == "/catalog-admin/groups" and method == "GET":
            catalog_conn = open_db(db_path)
            try:
                groups = list_catalog_groups(catalog_conn)
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
                catalog_conn.close()

        if path == "/catalog-admin/groups" and method == "POST":
            catalog_conn = open_db(db_path)
            try:
                form_data = parse_post_data(environ)
                action = form_data.get("action", "")
                if action == "create_group":
                    create_catalog_group(
                        catalog_conn,
                        name=form_data.get("name", "").strip() or "Новая группа",
                        sort_order=int(form_data.get("sort_order", "999") or 999),
                        status=form_data.get("status", "active"),
                    )
                elif action == "update_group":
                    update_catalog_group(
                        catalog_conn,
                        group_id=int(form_data["group_id"]),
                        name=form_data.get("name", "").strip() or "Группа",
                        sort_order=int(form_data.get("sort_order", "999") or 999),
                        status=form_data.get("status", "active"),
                    )
                elif action == "remove_group":
                    remove_catalog_group(catalog_conn, int(form_data["group_id"]))
                return redirect_response(start_response, "/catalog-admin/groups")
            finally:
                catalog_conn.close()

        if path.startswith("/catalog-admin/groups/"):
            group_id = parse_path_int(path, "/catalog-admin/groups/")
            if group_id is None:
                return not_found(start_response)

            catalog_conn = open_db(db_path)
            try:
                if method == "POST":
                    form_data = parse_post_data(environ)
                    action = form_data.get("action", "")
                    if action == "update_group":
                        update_catalog_group(
                            catalog_conn,
                            group_id=group_id,
                            name=form_data.get("name", "").strip() or "Группа",
                            sort_order=int(form_data.get("sort_order", "999") or 999),
                            status=form_data.get("status", "active"),
                        )
                    elif action == "create_skill":
                        create_catalog_skill(
                            catalog_conn,
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
                            remove_catalog_skill(catalog_conn, skill_id)
                    elif action == "remove_group":
                        remove_catalog_group(catalog_conn, group_id)
                        return redirect_response(start_response, "/catalog-admin/groups")
                    return redirect_response(start_response, f"/catalog-admin/groups/{group_id}")

                group = get_catalog_group(catalog_conn, group_id)
                if not group:
                    return not_found(start_response, "Group not found")
                skills = list_catalog_group_skills(catalog_conn, group_id)
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
                catalog_conn.close()

        if path.startswith("/catalog-admin/skills/"):
            skill_id = parse_path_int(path, "/catalog-admin/skills/")
            if skill_id is None:
                return not_found(start_response)

            from spravochnik_intake.pipeline import competency_catalog

            catalog_conn = open_db(db_path)
            try:
                if method == "POST":
                    form_data = parse_post_data(environ)
                    action = form_data.get("action", "")
                    if action == "update_skill":
                        update_catalog_skill(
                            catalog_conn,
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
                        skill = get_catalog_skill(catalog_conn, skill_id)
                        group_id = skill["group_id"] if skill else None
                        remove_catalog_skill(catalog_conn, skill_id)
                        if group_id is not None:
                            return redirect_response(start_response, f"/catalog-admin/groups/{group_id}")
                        return redirect_response(start_response, "/catalog-admin/groups")
                    elif action == "add_alias":
                        add_skill_alias(
                            catalog_conn,
                            skill_id=skill_id,
                            alias=form_data.get("alias", ""),
                            source="manual",
                        )
                    elif action == "remove_alias":
                        alias_id = int(form_data.get("alias_id", "0") or 0)
                        if alias_id:
                            remove_skill_alias(catalog_conn, skill_id, alias_id)
                    elif action == "merge_skill":
                        target_skill_id = int(form_data.get("target_skill_id", "0") or 0)
                        if target_skill_id:
                            merge_catalog_skills(catalog_conn, skill_id, target_skill_id)
                            return redirect_response(start_response, f"/catalog-admin/skills/{target_skill_id}")
                    elif action == "link_competency":
                        skill = get_catalog_skill(catalog_conn, skill_id)
                        if skill:
                            competency_catalog.ensure_skill_competency_link(
                                catalog_conn,
                                skill_id=skill_id,
                                skill_name=str(skill.get("name") or "Skill"),
                                competency_title=form_data.get("competency_title", ""),
                                indicators=None,
                                source_note="manual_catalog_admin",
                            )
                            catalog_conn.commit()
                    elif action == "unlink_competency":
                        competency_skill_id = int(form_data.get("competency_skill_id", "0") or 0)
                        if competency_skill_id:
                            competency_catalog.remove_competency_skill_link(catalog_conn, competency_skill_id)
                    elif action == "create_indicator":
                        create_catalog_indicator(
                            catalog_conn,
                            skill_id=skill_id,
                            indicator_type=form_data.get("indicator_type", "Не указано"),
                            text=form_data.get("text", "").strip() or "Новый индикатор",
                            sort_order=int(form_data.get("sort_order", "999") or 999),
                            complexity_band=form_data.get("complexity_band", ""),
                            is_active=1 if form_data.get("is_active", "1") == "1" else 0,
                        )
                    elif action == "update_indicator":
                        update_catalog_indicator(
                            catalog_conn,
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
                            remove_catalog_indicator(catalog_conn, indicator_id)
                    return redirect_response(start_response, f"/catalog-admin/skills/{skill_id}")

                skill = get_catalog_skill(catalog_conn, skill_id)
                if not skill:
                    return not_found(start_response, "Skill not found")
                indicators = list_catalog_indicators(catalog_conn, skill_id)
                aliases = list_skill_aliases(catalog_conn, skill_id)
                merge_query = (query_params.get("merge_query") or [""])[0].strip()
                merge_candidates = search_catalog_skills(catalog_conn, merge_query, exclude_skill_id=skill_id) if merge_query else []
                competency_query = (query_params.get("competency_query") or [""])[0].strip()
                competency_links = competency_catalog.list_skill_competency_links(catalog_conn, skill_id)
                competency_options = competency_catalog.list_competency_options(catalog_conn, competency_query)
                html = render(
                    "catalog_admin_skill_detail.html",
                    {
                        "title": skill["name"],
                        "skill": skill,
                        "indicators": indicators,
                        "aliases": aliases,
                        "merge_query": merge_query,
                        "merge_candidates": merge_candidates,
                        "competency_query": competency_query,
                        "competency_links": competency_links,
                        "competency_options": competency_options,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)
            finally:
                catalog_conn.close()

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

            if path == "/reviews/apply-catalog" and method == "POST":
                ensure_intake_runtime_schema(conn, db_path)
                form_data = parse_post_data(environ)
                try:
                    brief_id = int(form_data.get("brief_id", "0"))
                except ValueError:
                    return not_found(start_response, "Invalid brief id")
                apply_result = apply_brief_catalog_decisions(conn, brief_id)
                latest_job_id = apply_result.get("dag_state", {}).get("latest_job_id") if isinstance(apply_result.get("dag_state"), dict) else None
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
                for key in ("severity", "reason", "entity_type"):
                    value = form_data.get(key, "")
                    if value:
                        redirect_parts.append(f"{key}={value}")
                location = "/reviews"
                if redirect_parts:
                    location += "?" + "&".join(redirect_parts)
                return redirect_response(start_response, location)

            if path == "/intake/jobs/clear" and method == "POST":
                ensure_intake_runtime_schema(conn, db_path)
                clear_intake_workspace(conn)
                return redirect_response(start_response, "/intake")

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
                competency_id = parse_path_int(path, "/competencies/")
                if competency_id is None:
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
                include_service_profiles = query_params.get("service", ["0"])[0] == "1"
                profiles = list_profiles(conn, include_service=include_service_profiles)
                html = render(
                    "profiles.html",
                    {
                        "title": "Профили",
                        "profiles": profiles,
                        "include_service_profiles": include_service_profiles,
                        "request_path": path,
                    },
                )
                return html_response(start_response, html)

            if path.startswith("/profiles/"):
                profile_id = parse_path_int(path, "/profiles/")
                if profile_id is None:
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
                entity_type_filter = query_params.get("entity_type", ["all"])[0]
                status_totals, breakdown, items, reason_codes, entity_type_codes = list_reviews(
                    conn,
                    status_filter=status_filter,
                    severity_filter=severity_filter,
                    reason_filter=reason_filter,
                    entity_type_filter=entity_type_filter,
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
                        "entity_type_filter": entity_type_filter,
                        "reason_codes": reason_codes,
                        "entity_type_codes": entity_type_codes,
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

            if path == "/up/cleanup-empty" and method == "POST":
                ensure_intake_runtime_schema(conn, db_path)
                cleanup_empty_curriculum_plans(conn)
                return redirect_response(start_response, "/up")

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

                if len(segments) == 4 and segments[3] == "delete" and method == "POST":
                    delete_curriculum_plan(conn, plan_id)
                    return redirect_response(start_response, "/up")

                if len(segments) == 4 and segments[3] == "csv" and method == "GET":
                    plan_payload = get_curriculum_plan(conn, plan_id)
                    if not plan_payload:
                        return not_found(start_response, "Curriculum plan not found")
                    if str(plan_payload.get("status") or "").casefold() == "invalid":
                        return response(
                            start_response,
                            "CSV export is blocked: curriculum plan has DAG order violations.".encode("utf-8"),
                            status="409 Conflict",
                            content_type="text/plain; charset=utf-8",
                        )
                    filename = f"curriculum_plan_{plan_id}.csv"
                    return response(
                        start_response,
                        curriculum_plan_to_csv_bytes(plan_payload),
                        content_type="text/csv; charset=utf-8",
                        headers=[("Content-Disposition", f'attachment; filename="{filename}"')],
                    )

                if len(segments) == 5 and segments[3] == "template-proposals" and segments[4] == "generate" and method == "POST":
                    from spravochnik_intake.pipeline import storage as intake_storage
                    from spravochnik_intake.pipeline import llm as intake_llm

                    plan_payload = get_curriculum_plan(conn, plan_id)
                    if not plan_payload:
                        return not_found(start_response, "Curriculum plan not found")
                    brief_id = int(plan_payload.get("brief_id") or 0)
                    if not brief_id:
                        return not_found(start_response, "Curriculum plan has no brief")
                    try:
                        intake_llm.set_usage_context(brief_id=brief_id, stage="up_template_consilium")
                        intake_storage.generate_curriculum_artifact_template_proposals(
                            conn,
                            brief_id=brief_id,
                            plan_id=plan_id,
                        )
                    finally:
                        intake_llm.clear_usage_context()
                    return redirect_response(start_response, f"/up/plans/{plan_id}/template-proposals")

                if len(segments) == 4 and segments[3] == "template-proposals" and method == "GET":
                    from spravochnik_intake.pipeline import storage as intake_storage

                    plan_payload = get_curriculum_plan(conn, plan_id)
                    if not plan_payload:
                        return not_found(start_response, "Curriculum plan not found")
                    proposals = intake_storage.load_curriculum_artifact_template_proposals(
                        conn,
                        int(plan_payload.get("brief_id") or 0),
                    )
                    html = render(
                        "up_template_proposals.html",
                        {
                            "title": f"Предложения шаблонов УП #{plan_id}",
                            "plan": plan_payload,
                            "proposals": proposals,
                            "artifact_family_options": ARTIFACT_FAMILY_OPTIONS,
                            "scope_type_options": ARTIFACT_SCOPE_TYPE_OPTIONS,
                            "request_path": "/up",
                        },
                    )
                    return html_response(start_response, html)

                if len(segments) == 5 and segments[3] == "template-proposals" and method == "POST":
                    from spravochnik_intake.pipeline import storage as intake_storage

                    try:
                        proposal_id = int(segments[4])
                    except ValueError:
                        return not_found(start_response, "Invalid proposal id")
                    plan_payload = get_curriculum_plan(conn, plan_id)
                    if not plan_payload:
                        return not_found(start_response, "Curriculum plan not found")
                    form_data = parse_post_data(environ)
                    action = form_data.get("action", "")
                    redirect_plan_id = plan_id
                    if action in {"save_proposal", "accept_proposal"}:
                        scope_type = form_data.get("scope_type", "coverage_area").strip() or "coverage_area"
                        intake_storage.update_curriculum_artifact_template_proposal(
                            conn,
                            proposal_id,
                            title=form_data.get("title", "").strip(),
                            artifact_family=form_data.get("artifact_family", "practice").strip() or "practice",
                            scope_type=scope_type,
                            scope_names=parse_scope_names(form_data.get("scope_names"), scope_type),
                            artifact_description=form_data.get("artifact_description", "").strip(),
                            project_name_pattern=form_data.get("project_name_pattern", "").strip(),
                            materials_pattern=form_data.get("materials_pattern", "").strip(),
                            storytelling_pattern=form_data.get("storytelling_pattern", "").strip(),
                            validation_criteria=form_data.get("validation_criteria", "").strip(),
                            rationale=form_data.get("rationale", "").strip(),
                            confidence=parse_optional_float(form_data.get("confidence")),
                        )
                    if action == "accept_proposal":
                        intake_storage.accept_curriculum_artifact_template_proposal(conn, proposal_id)
                        brief_id = int(plan_payload.get("brief_id") or 0)
                        rebuilt = build_curriculum_plan_for_brief(conn, brief_id)
                        redirect_plan_id = int(rebuilt.get("plan_id") or plan_id)
                        conn.execute(
                            """
                            UPDATE curriculum_artifact_template_proposal
                            SET plan_id = COALESCE(plan_id, ?)
                            WHERE brief_id = ?
                            """,
                            (redirect_plan_id, brief_id),
                        )
                        conn.commit()
                    elif action == "reject_proposal":
                        intake_storage.reject_curriculum_artifact_template_proposal(conn, proposal_id)
                    return redirect_response(start_response, f"/up/plans/{redirect_plan_id}/template-proposals")

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
                job_id = parse_path_int(path, "/intake/jobs/", "/status")
                if job_id is None:
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

            if path.startswith("/intake/jobs/") and path.endswith("/next-step") and method == "POST":
                job_id = parse_path_int(path, "/intake/jobs/", "/next-step")
                if job_id is None:
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
                workspace_state = build_intake_workspace_state(conn, job, result, dag_build_state)
                next_step = workspace_state.get("next_step") if isinstance(workspace_state, dict) else None
                next_code = str(next_step.get("code") or "") if isinstance(next_step, dict) else ""
                brief_id = workspace_state.get("brief_id") if isinstance(workspace_state, dict) else None
                brief_id = brief_id if isinstance(brief_id, int) else None

                if next_code == "apply_catalog" and brief_id is not None:
                    apply_brief_catalog_decisions(conn, brief_id)
                    return redirect_response(start_response, f"/intake/jobs/{job_id}")
                if next_code == "build_dag" and brief_id is not None:
                    build_result = build_dag_for_brief(conn, brief_id)
                    latest_job_id = build_result["state"].get("latest_job_id") or job_id
                    return redirect_response(start_response, f"/intake/jobs/{latest_job_id}")
                if isinstance(next_step, dict) and next_step.get("href"):
                    return redirect_response(start_response, str(next_step["href"]))
                return redirect_response(start_response, f"/intake/jobs/{job_id}")

            if path.startswith("/intake/jobs/") and path.endswith("/build-dag") and method == "POST":
                job_id = parse_path_int(path, "/intake/jobs/", "/build-dag")
                if job_id is None:
                    return not_found(start_response)
                ensure_intake_runtime_schema(conn, db_path)
                job, brief_id = get_intake_job_brief_id(conn, job_id)
                if not job:
                    return not_found(start_response, "Intake job not found")
                if brief_id is None:
                    return not_found(start_response, "Brief id not found")
                build_result = build_dag_for_brief(conn, brief_id)
                latest_job_id = build_result["state"].get("latest_job_id") or job_id
                return redirect_response(start_response, f"/intake/jobs/{latest_job_id}")

            if path.startswith("/intake/jobs/") and path.endswith("/apply-catalog") and method == "POST":
                job_id = parse_path_int(path, "/intake/jobs/", "/apply-catalog")
                if job_id is None:
                    return not_found(start_response)
                ensure_intake_runtime_schema(conn, db_path)
                job, brief_id = get_intake_job_brief_id(conn, job_id)
                if not job:
                    return not_found(start_response, "Intake job not found")
                if brief_id is None:
                    return not_found(start_response, "Brief id not found")
                apply_brief_catalog_decisions(conn, brief_id)
                return redirect_response(start_response, f"/intake/jobs/{job_id}")

            if path.startswith("/intake/jobs/") and path.endswith("/candidate-decision") and method == "POST":
                job_id = parse_path_int(path, "/intake/jobs/", "/candidate-decision")
                if job_id is None:
                    return not_found(start_response)
                ensure_intake_runtime_schema(conn, db_path)
                form_data = parse_post_data(environ)
                try:
                    suggestion_id = int(form_data.get("suggestion_id", "0"))
                except ValueError:
                    return not_found(start_response, "Invalid suggestion id")
                action = form_data.get("candidate_action", "")
                if action not in {"accept", "link", "reject", "review"}:
                    return not_found(start_response, "Invalid candidate action")
                target_decision = "needs_review"
                resolution_note = "Возвращено на review из intake-таблицы."
                if action == "accept":
                    target_decision = "accepted"
                    resolution_note = "Подтверждено из intake-таблицы."
                elif action == "link":
                    from spravochnik_intake.pipeline import storage as intake_storage

                    link_result = intake_storage.link_suggestion_to_nearest(conn, suggestion_id)
                    if link_result.get("status") != "linked":
                        return not_found(start_response, "Nearest catalog skill not found")
                    target_decision = "accepted"
                    resolution_note = f"Покрыто существующим skill: {link_result.get('canonical_name') or link_result.get('skill_id')}."
                elif action == "reject":
                    target_decision = "rejected"
                    resolution_note = "Отклонено из intake-таблицы."
                wants_json = (
                    "application/json" in str(environ.get("HTTP_ACCEPT", ""))
                    or str(environ.get("HTTP_X_REQUESTED_WITH", "")) == "fetch"
                )
                apply_candidate_decision(
                    conn,
                    suggestion_id,
                    target_decision,
                    resolution_note,
                )
                if wants_json:
                    return json_response(
                        start_response,
                        {
                            "ok": True,
                            "suggestion_id": suggestion_id,
                            "decision": target_decision,
                            "message": "Решение сохранено. После завершения проверки примените принятые навыки в справочник.",
                        },
                    )
                return redirect_response(start_response, f"/intake/jobs/{job_id}")

            if path.startswith("/intake/jobs/") and path.endswith("/plan.csv") and method == "GET":
                job_id = parse_path_int(path, "/intake/jobs/", "/plan.csv")
                if job_id is None:
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
                job_id = parse_path_int(path, "/intake/jobs/")
                if job_id is None:
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
                workflow_steps = build_intake_workflow_steps(job, result, dag_build_state)
                workspace_state = build_intake_workspace_state(conn, job, result, dag_build_state)
                llm_usage = load_llm_usage_summary(job_id)
                job_observability = build_job_observability(llm_usage)
                quality_metrics = build_intake_quality_metrics(result, llm_usage)
                html = render(
                    "intake.html",
                    {
                        "title": f"Бриф #{job_id}",
                        "brief": job.get("brief_text", ""),
                        "brief_file_path": normalize_existing_brief_file_path(job.get("file_path", "")),
                        "job": job,
                        "recent_jobs": list_recent_intake_jobs(conn),
                        "result": result,
                        "llm_usage": llm_usage,
                        "job_observability": job_observability,
                        "quality_metrics": quality_metrics,
                        "dag_build_state": dag_build_state,
                        "workflow_steps": workflow_steps,
                        "workspace_state": workspace_state,
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
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8010, help="Port to bind.")
    args = parser.parse_args()

    app = create_app(args.db.resolve(), args.summary.resolve())
    with make_server(args.host, args.port, app) as server:
        print(f"Catalog UI listening on http://{args.host}:{args.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
