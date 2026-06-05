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

from spravochnik_intake.pipeline import config, stage_atomize, stage_brief_to_catalog, stage_dag_to_up, storage
from spravochnik_intake.pipeline.catalog_repo import CatalogRepo
from spravochnik_intake.pipeline.models import IndicatorSpec, SkillCandidate
from spravochnik_intake.pipeline.skill_names import canonicalize_skill_name
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


def test_accept_promotes_neutral_name_and_original_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "USE_LIVE", False)
    db_path = _runtime_db_path("neutral")
    conn = _create_base_catalog_db(db_path)
    try:
        brief_id = storage.save_brief(conn, "brief", {"role": "роль", "seniority": "junior", "domain": "домен"})
        candidate = _candidate("Формулирование ценностного предложения")
        candidate.source_name = "Сформулировать ценностное предложение"
        suggestion_id = storage.save_suggestions(conn, brief_id, [candidate], {})["tmp-Формулирование ценностного предложения"]

        apply_candidate_decision(conn, suggestion_id, "accepted", "accepted in test")

        suggestion = conn.execute(
            "SELECT canonical_skill_id FROM skill_suggestion WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        skill = conn.execute("SELECT canonical_name FROM skill WHERE id = ?", (suggestion["canonical_skill_id"],)).fetchone()
        aliases = [
            row["alias"]
            for row in conn.execute(
                "SELECT alias FROM skill_alias WHERE skill_id = ? ORDER BY alias",
                (suggestion["canonical_skill_id"],),
            )
        ]

        assert skill["canonical_name"] == "Формулирование ценностного предложения"
        assert "Сформулировать ценностное предложение" in aliases
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


def test_up_planner_keeps_direct_edges_out_of_same_project() -> None:
    candidates = [
        _candidate("A base", group="theme", bloom="apply", decision="accepted"),
        _candidate("B depends", group="theme", bloom="apply", decision="accepted"),
        _candidate("C independent", group="theme", bloom="apply", decision="accepted"),
    ]
    candidates[0].tmp_id = "A"
    candidates[1].tmp_id = "B"
    candidates[2].tmp_id = "C"
    dag_payload = {
        "order": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
        "final_edges": [
            {
                "src_id": "A",
                "dst_id": "B",
                "src": "A base",
                "dst": "B depends",
                "relation_type": "hard",
                "confidence": 0.9,
            }
        ],
    }

    plan = stage_dag_to_up.run({"role": "tester", "seniority": "junior"}, candidates, dag_payload)

    assert plan["status"] == "built"
    assert plan["report"]["order_violations"] == []
    assert plan["report"]["project_violations"] == []
    assert not any({"A", "B"}.issubset(set(row["node_ids"])) for row in plan["rows"])


def test_up_planner_marks_inconsistent_topological_order_invalid() -> None:
    candidates = [
        _candidate("A base", group="theme", bloom="apply", decision="accepted"),
        _candidate("B depends", group="theme", bloom="apply", decision="accepted"),
    ]
    candidates[0].tmp_id = "A"
    candidates[1].tmp_id = "B"
    dag_payload = {
        "order": [{"id": "B"}, {"id": "A"}],
        "final_edges": [
            {
                "src_id": "A",
                "dst_id": "B",
                "src": "A base",
                "dst": "B depends",
                "relation_type": "hard",
                "confidence": 0.9,
            }
        ],
    }

    plan = stage_dag_to_up.run({"role": "tester", "seniority": "junior"}, candidates, dag_payload)

    assert plan["status"] == "invalid"
    assert plan["report"]["order_violations"] == ["A base -> B depends"]


def test_up_planner_builds_integrative_projects_and_quality_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_SPIRAL_ENABLED", True)
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    monkeypatch.setattr(config, "UP_TARGET_OUTCOMES_MIN", 3)
    candidates = [
        _candidate("A discovery", group="theme", bloom="apply", decision="accepted"),
        _candidate("B interview", group="theme", bloom="apply", decision="accepted"),
        _candidate("C map", group="theme", bloom="analyze", decision="accepted"),
    ]
    for index, candidate in enumerate(candidates, start=1):
        candidate.tmp_id = f"S{index}"
    dag_payload = {
        "order": [{"id": "S1"}, {"id": "S2"}, {"id": "S3"}],
        "final_edges": [],
    }

    plan = stage_dag_to_up.run({"role": "tester", "seniority": "junior"}, candidates, dag_payload)

    assert plan["status"] == "built"
    assert plan["rows"][0]["node_ids"] == ["S1", "S2", "S3"]
    assert plan["rows"][0]["outcome_count"] >= 3
    assert plan["report"]["quality_metrics"]["avg_skills_per_project"] == 3.0
    assert plan["report"]["quality_metrics"]["single_skill_project_count"] == 0


def test_up_planner_localizes_groups_and_keeps_block_titles_compact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    candidates = [
        _candidate("Identify jurisdictional requirements", group="Legal & admin", bloom="apply", decision="accepted"),
        _candidate("Prepare basic legal documents", group="Legal & admin", bloom="apply", decision="accepted"),
    ]
    for index, candidate in enumerate(candidates, start=1):
        candidate.tmp_id = f"S{index}"
    dag_payload = {
        "order": [{"id": "S1"}, {"id": "S2"}],
        "final_edges": [],
    }

    plan = stage_dag_to_up.run({"role": "tester", "seniority": "junior"}, candidates, dag_payload)

    assert plan["status"] == "built"
    assert "Право и администрирование" in plan["rows"][0]["block_title"]
    assert "Legal" not in plan["rows"][0]["block_title"]
    assert len(plan["rows"][0]["block_title"]) <= 80
    assert "Подготовка базовых юридических документов" in plan["rows"][0]["project_name"]


def test_up_planner_allows_soft_edges_inside_integrative_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    candidates = [
        _candidate("A base", group="theme", bloom="apply", decision="accepted"),
        _candidate("B follows", group="theme", bloom="apply", decision="accepted"),
    ]
    candidates[0].tmp_id = "A"
    candidates[1].tmp_id = "B"
    dag_payload = {
        "order": [{"id": "A"}, {"id": "B"}],
        "final_edges": [
            {
                "src_id": "A",
                "dst_id": "B",
                "src": "A base",
                "dst": "B follows",
                "relation_type": "soft",
                "confidence": 0.95,
            }
        ],
    }

    plan = stage_dag_to_up.run({"role": "tester", "seniority": "junior"}, candidates, dag_payload)

    assert plan["status"] == "built"
    assert any({"A", "B"}.issubset(set(row["node_ids"])) for row in plan["rows"])
    assert plan["report"]["project_violations"] == []


def test_up_planner_adds_spiral_thread_occurrence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_SPIRAL_ENABLED", True)
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    monkeypatch.setattr(config, "UP_MIN_THREAD_OCCURRENCES", 2)
    monkeypatch.setattr(config, "UP_MAX_THREAD_OCCURRENCES", 2)
    monkeypatch.setattr(config, "UP_SPIRAL_MIN_GAP", 2)
    candidates = [
        _candidate("A core", group="theme", bloom="apply", decision="accepted"),
        _candidate("B depends", group="theme", bloom="apply", decision="accepted"),
        _candidate("C independent", group="theme", bloom="apply", decision="accepted"),
        _candidate("D independent", group="theme", bloom="apply", decision="accepted"),
        _candidate("E independent", group="theme", bloom="apply", decision="accepted"),
        _candidate("F late", group="theme", bloom="analyze", decision="accepted"),
    ]
    for index, candidate in enumerate(candidates, start=1):
        candidate.tmp_id = f"S{index}"
    dag_payload = {
        "order": [{"id": f"S{index}"} for index in range(1, 7)],
        "final_edges": [
            {
                "src_id": "S1",
                "dst_id": "S2",
                "src": "A core",
                "dst": "B depends",
                "relation_type": "hard",
                "confidence": 0.9,
            }
        ],
    }

    plan = stage_dag_to_up.run({"role": "tester", "seniority": "junior"}, candidates, dag_payload)

    assert plan["status"] == "built"
    assert plan["report"]["quality_metrics"]["repeated_thread_count"] >= 1
    assert any("контроль/владение" in row["skills_list"] or "закрепление" in row["skills_list"] for row in plan["rows"])
    assert plan["report"]["project_violations"] == []


def test_intro_bloom_create_is_clamped_without_explicit_signal() -> None:
    seniority = "\u043d\u0430\u0447\u0438\u043d\u0430\u044e\u0449\u0438\u0439"
    routine = "\u0424\u043e\u0440\u043c\u0443\u043b\u0438\u0440\u0443\u0435\u0442 A/B \u0433\u0438\u043f\u043e\u0442\u0435\u0437\u044b"
    explicit = "\u0421\u043e\u0437\u0434\u0430\u0451\u0442 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u0430"

    assert stage_brief_to_catalog.normalize_bloom("create", {"seniority": seniority}, routine) == "analyze"
    assert stage_brief_to_catalog.normalize_bloom("create", {"seniority": seniority}, explicit) == "create"


def test_triage_does_not_mark_matched_skill_as_novel() -> None:
    candidate = _candidate("Existing skill", decision="needs_review")
    candidate.resolution = "matched"
    candidate.match_score = 100.0
    candidate.confidence = 0.98
    candidate.evidence_ids = []

    stage_brief_to_catalog.triage_candidates([candidate], {"artifact_type": "program_brief"})

    assert "novel_skill" not in candidate.reasons
    assert "single_source" not in candidate.reasons


def test_atomize_batches_live_suspicious_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "USE_LIVE", True)
    calls: list[list[str]] = []

    def fake_batch(cands: list[SkillCandidate]) -> dict[str, dict[str, object]]:
        calls.append([cand.tmp_id for cand in cands])
        return {cand.tmp_id: {"verdict": "atomic", "rationale": "batch"} for cand in cands}

    def fail_single(_cand: SkillCandidate) -> dict[str, object]:
        raise AssertionError("single atomize call should not be used when batch returns all decisions")

    monkeypatch.setattr(stage_atomize, "_call_live_batch", fake_batch)
    monkeypatch.setattr(stage_atomize, "_call_live", fail_single)
    candidates = [
        _candidate("Очень длинная формулировка навыка для проверки атомизации", decision="needs_review"),
        _candidate("Ещё одна длинная формулировка навыка для атомизации", decision="needs_review"),
    ]

    result = stage_atomize.run(candidates)

    assert calls == [[candidates[0].tmp_id, candidates[1].tmp_id]]
    assert [candidate.atomicity for candidate in result] == ["atomic", "atomic"]


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


def test_link_suggestion_to_nearest_uses_existing_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "USE_LIVE", False)
    db_path = _runtime_db_path("nearest-link")
    conn = _create_base_catalog_db(db_path)
    try:
        group_id = ensure_catalog_group(conn, "research", "Research", 1)
        existing_skill_id = create_catalog_skill(conn, group_id, "Проведение клиентского интервью", 1, "", "", "manual", "", 1)
        brief_id = storage.save_brief(conn, "brief", {"role": "роль", "seniority": "junior", "domain": "домен"})
        candidate = _candidate("Проведение клиентские интервью", decision="needs_review")
        candidate.nearest_skill_id = existing_skill_id
        candidate.nearest_name = "Проведение клиентского интервью"
        candidate.match_score = 82.0
        suggestion_id = storage.save_suggestions(conn, brief_id, [candidate], {})["tmp-Проведение клиентские интервью"]
        before_skill_count = conn.execute("SELECT COUNT(*) FROM skill").fetchone()[0]

        link_result = storage.link_suggestion_to_nearest(conn, suggestion_id)
        apply_candidate_decision(conn, suggestion_id, "accepted", "linked in test")

        after_skill_count = conn.execute("SELECT COUNT(*) FROM skill").fetchone()[0]
        suggestion = conn.execute("SELECT decision, resolution, canonical_skill_id FROM skill_suggestion WHERE id = ?", (suggestion_id,)).fetchone()
        alias = conn.execute(
            "SELECT alias FROM skill_alias WHERE skill_id = ? AND alias = ?",
            (existing_skill_id, "Проведение клиентские интервью"),
        ).fetchone()

        assert link_result["status"] == "linked"
        assert before_skill_count == after_skill_count
        assert suggestion["decision"] == "accepted"
        assert suggestion["resolution"] == "alias"
        assert suggestion["canonical_skill_id"] == existing_skill_id
        assert alias is not None
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_skill_name_canonicalization_and_resolve_source_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "USE_LIVE", False)
    assert canonicalize_skill_name("Сформулировать ценностное предложение") == "Формулирование ценностного предложения"
    assert canonicalize_skill_name("Провести глубинное интервью") == "Проведение глубинных интервью"

    db_path = _runtime_db_path("source-resolve")
    conn = _create_base_catalog_db(db_path)
    try:
        brief_id = storage.save_brief(conn, "brief", {"role": "роль", "seniority": "junior", "domain": "домен"})
        original = _candidate("Провести глубинное интервью", decision="needs_review")
        suggestion_id = storage.save_suggestions(conn, brief_id, [original], {})["tmp-Провести глубинное интервью"]
        apply_candidate_decision(conn, suggestion_id, "accepted", "accepted")

        repo = CatalogRepo(str(db_path))
        try:
            candidate = _candidate("Проведение глубинных интервью")
            candidate.source_name = "Провести глубинное интервью"
            candidate.resolution = None
            repo.resolve(candidate)
        finally:
            repo.close()

        assert candidate.resolution in {"matched", "alias"}
        assert candidate.canonical_skill_id is not None
        assert candidate.match_score == 100.0
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)
