from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from spravochnik_intake.pipeline import config, storage
from spravochnik_intake.pipeline.catalog_repo import CatalogRepo
from spravochnik_intake.pipeline.models import IndicatorSpec, SkillCandidate
from viewer.app import (
    apply_candidate_decision,
    build_dag_for_brief,
    build_intake_workflow_steps,
    create_intake_job,
    create_catalog_indicator,
    create_catalog_skill,
    curriculum_plan_to_csv_bytes,
    ensure_catalog_group,
    ensure_intake_runtime_schema,
    get_intake_job,
    merge_catalog_skills,
    open_db,
    update_intake_job,
)

RUNTIME_DIR = PROJECT_ROOT / "test_runtime"
RUNTIME_DIR.mkdir(exist_ok=True)


def _runtime_db_path(prefix: str) -> Path:
    return RUNTIME_DIR / f"{prefix}-{uuid.uuid4().hex}.sqlite"


def _create_base_catalog_db(db_path: Path) -> sqlite3.Connection:
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE skill (
            id INTEGER PRIMARY KEY,
            normalized_name TEXT NOT NULL UNIQUE,
            canonical_name TEXT NOT NULL,
            skill_type TEXT NOT NULL DEFAULT 'unknown',
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE skill_alias (
            id INTEGER PRIMARY KEY,
            skill_id INTEGER NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            UNIQUE(skill_id, normalized_alias)
        );

        CREATE TABLE review_queue (
            id INTEGER PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            source_ref TEXT,
            reason_code TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            details TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            resolution_note TEXT,
            reviewed_at TEXT,
            updated_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    raw.commit()
    raw.close()

    conn = open_db(db_path)
    ensure_intake_runtime_schema(conn, db_path)
    return conn


def _candidate(name: str, *, group: str = "Тестовая группа", bloom: str = "apply", decision: str = "needs_review") -> SkillCandidate:
    return SkillCandidate(
        tmp_id=f"tmp-{name}",
        name=name,
        group=group,
        coverage_area=group,
        indicators=[IndicatorSpec(text=f"Применяет: {name}", bloom=bloom)],
        tools=[],
        resolution="new",
        confidence=0.98,
        council_agreement=1.0,
        entity_type="skill",
        atomicity="atomic",
        decision=decision,
        reasons=["novel_skill"] if decision == "needs_review" else [],
    )


def test_accept_promotes_skill_and_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "USE_LIVE", False)
    db_path = _runtime_db_path("accept")
    conn = _create_base_catalog_db(db_path)
    try:
        brief_id = storage.save_brief(conn, "brief", {"role": "роль", "seniority": "junior", "domain": "домен"})
        suggestion_id = storage.save_suggestions(conn, brief_id, [_candidate("Методологический smoke skill")], {})[
            "tmp-Методологический smoke skill"
        ]

        changed_brief_id = apply_candidate_decision(conn, suggestion_id, "accepted", "accepted in test")

        assert changed_brief_id == brief_id
        suggestion = conn.execute(
            "SELECT decision, resolution, canonical_skill_id FROM skill_suggestion WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        assert suggestion["decision"] == "accepted"
        assert suggestion["resolution"] in {"matched", "alias"}
        assert suggestion["canonical_skill_id"] is not None

        skill = conn.execute("SELECT canonical_name, status, is_active FROM skill WHERE id = ?", (suggestion["canonical_skill_id"],)).fetchone()
        assert skill["canonical_name"] == "Методологический smoke skill"
        assert skill["status"] == "active"
        assert int(skill["is_active"]) == 1

        alias = conn.execute(
            "SELECT source FROM skill_alias WHERE skill_id = ? AND alias = ?",
            (suggestion["canonical_skill_id"], "Методологический smoke skill"),
        ).fetchone()
        assert alias["source"] == "intake_accept"

        review = conn.execute("SELECT status, resolution_note FROM review_queue WHERE entity_id = ?", (suggestion_id,)).fetchone()
        assert review["status"] == "resolved"
        assert review["resolution_note"] == "accepted in test"
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_merge_moves_aliases_indicators_and_archives_source() -> None:
    db_path = _runtime_db_path("merge")
    conn = _create_base_catalog_db(db_path)
    try:
        group_id = ensure_catalog_group(conn, "backend", "Backend", 1)
        source_id = create_catalog_skill(conn, group_id, "SQL запросы", 1, "", "", "manual", "", 1)
        target_id = create_catalog_skill(conn, group_id, "Работа с SQL", 2, "", "", "manual", "", 1)
        create_catalog_indicator(conn, source_id, "Умеет", "Пишет SELECT-запросы", 1, "junior", 1)

        result = merge_catalog_skills(conn, source_id, target_id)

        assert result["status"] == "merged"
        source = conn.execute("SELECT status, is_active FROM skill WHERE id = ?", (source_id,)).fetchone()
        assert source["status"] == "deprecated"
        assert int(source["is_active"]) == 0
        assert conn.execute("SELECT COUNT(*) FROM indicator WHERE skill_id = ?", (target_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM skill_alias WHERE skill_id = ?", (target_id,)).fetchone()[0] >= 1
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_dag_rebuild_persists_edges_and_curriculum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "USE_LIVE", False)
    db_path = _runtime_db_path("dag")
    conn = _create_base_catalog_db(db_path)
    try:
        brief_id = storage.save_brief(conn, "backend brief", {"role": "Backend", "seniority": "junior", "domain": "IT"})
        candidates = [
            _candidate("Работа с реляционными БД", bloom="understand", decision="accepted"),
            _candidate("SQL запросы", bloom="apply", decision="accepted"),
        ]
        storage.save_suggestions(conn, brief_id, candidates, {})

        result = build_dag_for_brief(conn, brief_id)

        assert result["dag"]["status"] == "built"
        assert int(result["dag"]["nodes"]) == 2
        assert conn.execute("SELECT COUNT(*) FROM skill_prerequisite WHERE brief_id = ?", (brief_id,)).fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM curriculum_plan_row cpr JOIN curriculum_plan cp ON cp.id = cpr.plan_id WHERE cp.brief_id = ?", (brief_id,)).fetchone()[0] >= 1
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_curriculum_csv_writes_a_to_v_and_keeps_o_to_v_empty() -> None:
    payload = {
        "csv_primary_header": [f"col-{letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUV"],
        "csv_secondary_header": [""] * 22,
        "rows": [
            {
                "block_title": "Блок",
                "block_goal": "Цель",
                "row_number": 1,
                "project_name": "Проект",
                "project_summary": "Описание",
                "outcomes_know": "Знает",
                "outcomes_can": "Умеет",
                "outcomes_skills": "Навык",
                "required_tools": "Python",
                "materials": "Материал",
                "storytelling": "История",
                "delivery_format": "индивидуальный",
                "group_size": 1,
                "effort_hours": 8,
                "effort_days": 99,
                "cumulative_days": 99,
                "xp": 999,
                "platform_project_name": "must not export",
                "artifact_links": "must not export",
            }
        ],
    }

    decoded = curriculum_plan_to_csv_bytes(payload).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(decoded)))

    assert len(rows[0]) == 22
    assert len(rows[1]) == 22
    assert len(rows[2]) == 22
    assert rows[2][0:14] == [
        "Блок",
        "Цель",
        "1",
        "Проект",
        "Описание",
        "Знает",
        "Умеет",
        "Навык",
        "Python",
        "Материал",
        "История",
        "индивидуальный",
        "1",
        "8",
    ]
    assert rows[2][14:] == [""] * 8


def test_intake_status_labels_and_workflow_steps() -> None:
    db_path = _runtime_db_path("status")
    conn = _create_base_catalog_db(db_path)
    try:
        job_id = create_intake_job(
            conn,
            source_kind="text",
            source_name=None,
            file_path=None,
            brief_text="brief",
            use_council=False,
        )
        update_intake_job(conn, job_id, status="running", current_stage="search", progress_note="gray-zone search", mark_started=True)

        job = get_intake_job(conn, job_id)
        assert job is not None
        assert job["status_label"] == "Обрабатывается"
        assert job["current_stage_label"] == "Поиск evidence по серой зоне"

        steps = build_intake_workflow_steps(job, None, None)
        assert [step["label"] for step in steps] == ["Бриф", "Проверка", "Справочник пополнен", "УП"]
        assert steps[1]["status"] == "active"
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_catalog_accumulation_resolves_promoted_skill_as_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "USE_LIVE", False)
    db_path = _runtime_db_path("accumulation")
    conn = _create_base_catalog_db(db_path)
    try:
        brief_id = storage.save_brief(conn, "brief", {"role": "роль", "seniority": "junior", "domain": "домен"})
        skill_name = "Повторно используемый каталоговый skill"
        suggestion_id = storage.save_suggestions(conn, brief_id, [_candidate(skill_name)], {})[f"tmp-{skill_name}"]
        apply_candidate_decision(conn, suggestion_id, "accepted", "accepted")

        repo = CatalogRepo(str(db_path))
        try:
            candidate = _candidate(skill_name)
            candidate.resolution = None
            repo.resolve(candidate)
        finally:
            repo.close()

        assert candidate.resolution == "matched"
        assert candidate.canonical_skill_id is not None
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)
