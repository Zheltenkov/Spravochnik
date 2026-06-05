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

from spravochnik_intake.pipeline import (
    config,
    stage_atomize,
    stage_brief_to_catalog,
    stage_catalog_to_dag,
    stage_dag_to_up,
    storage,
    up_template_consilium,
)
from spravochnik_intake.pipeline.catalog_repo import CatalogRepo
from spravochnik_intake.pipeline.curriculum import PlanNode, ProjectBlueprint, SkillOccurrence
from spravochnik_intake.pipeline.models import IndicatorSpec, SkillCandidate
from spravochnik_intake.pipeline.skill_names import canonicalize_skill_name
from viewer.app import (
    apply_candidate_decision,
    build_curriculum_plan_payload_from_rows,
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
    load_brief_spec_for_plan,
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


def test_up_planner_repairs_inconsistent_topological_order_with_hard_edges() -> None:
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

    assert plan["status"] == "built"
    assert plan["report"]["order_violations"] == []
    assert plan["report"]["project_violations"] == []
    assert [row["node_ids"] for row in plan["rows"]] == [["A"], ["B"]]


def test_up_planner_builds_integrative_projects_and_quality_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_SPIRAL_ENABLED", True)
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    monkeypatch.setattr(config, "UP_TARGET_OUTCOMES_MIN", 3)
    candidates = [
        _candidate("Анализ потребности", group="theme", bloom="analyze", decision="accepted"),
        _candidate("Оценка ограничений", group="theme", bloom="analyze", decision="accepted"),
        _candidate("Исследование контекста", group="theme", bloom="analyze", decision="accepted"),
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


def test_up_detail_payload_keeps_quality_metrics() -> None:
    plan_meta = {
        "id": 1,
        "status": "built",
        "title": "УП",
        "audience_level": "Начальный",
        "source_policy": "accepted_only",
        "payload_json": json.dumps(
            {
                "message": "built",
                "report": {
                    "coverage_ok": True,
                    "order_violations": [],
                    "project_violations": [],
                    "quality_metrics": {
                        "avg_skills_per_project": 2.0,
                        "avg_outcomes_per_project": 3.0,
                        "single_skill_project_count": 0,
                        "overloaded_project_count": 0,
                        "core_thread_count": 1,
                        "repeated_thread_count": 1,
                        "spiral_enabled": True,
                        "target_skills_per_project": [2, 4],
                        "target_outcomes_per_project": [3, 5],
                    },
                },
            },
            ensure_ascii=False,
        ),
    }
    rows = [
        {
            "id": 1,
            "block_index": 1,
            "row_number": 1,
            "block_title": "Блок",
            "block_goal": "Цель",
            "project_name": "Проект",
            "outcomes_know": "Знает",
            "outcomes_can": "Умеет",
            "outcomes_skills": "Владеет",
            "skills_list": "Skill A, Skill B",
            "effort_hours": 8,
            "effort_days": 1,
            "xp": 80,
        }
    ]

    payload = build_curriculum_plan_payload_from_rows(plan_meta, rows)

    assert payload["report"]["quality_metrics"]["avg_skills_per_project"] == 2.0
    assert payload["report"]["quality_metrics"]["avg_outcomes_per_project"] == 3.0
    assert payload["report"]["quality_metrics"]["repeated_thread_count"] == 1


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
    assert plan["rows"][0]["project_name"] == "Практический проект: Право и администрирование"


def test_up_planner_uses_dynamic_catalog_themes_without_local_archetypes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    candidates = [
        _candidate("Оценка кислотности почвы", group="Фермерство / Почва", bloom="apply", decision="accepted"),
        _candidate("Настройка полива теплицы", group="Фермерство / Теплицы", bloom="apply", decision="accepted"),
        _candidate("Планирование кормления стада", group="Животноводство", bloom="analyze", decision="accepted"),
        _candidate("Контроль санитарной обработки оборудования", group="Пищевая безопасность", bloom="apply", decision="accepted"),
    ]
    for index, candidate in enumerate(candidates, start=1):
        candidate.tmp_id = f"S{index}"
    dag_payload = {
        "order": [{"id": f"S{index}"} for index in range(1, len(candidates) + 1)],
        "final_edges": [],
    }

    plan = stage_dag_to_up.run({"role": "Агроном", "seniority": "начинающий"}, candidates, dag_payload)
    project_names = [row["project_name"] for row in plan["rows"]]

    assert plan["status"] == "built"
    assert plan["report"]["quality_metrics"]["artifact_first"] is True
    assert all(name.startswith("Практический проект:") for name in project_names)
    assert any("Фермерство" in name for name in project_names)
    assert any("Животноводство" in name for name in project_names)


def test_up_planner_splits_same_theme_by_artifact_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    candidates = [
        _candidate("Анализ качества почвы", group="Фермерство", bloom="analyze", decision="accepted"),
        _candidate("Настройка датчика влажности", group="Фермерство", bloom="apply", decision="accepted"),
        _candidate("Оценка кислотности почвы", group="Фермерство", bloom="analyze", decision="accepted"),
    ]
    for index, candidate in enumerate(candidates, start=1):
        candidate.tmp_id = f"S{index}"
    dag_payload = {
        "order": [{"id": "S1"}, {"id": "S2"}, {"id": "S3"}],
        "final_edges": [],
    }

    plan = stage_dag_to_up.run({"role": "Агроном", "seniority": "начинающий"}, candidates, dag_payload)

    assert plan["status"] == "built"
    assert any({"S1", "S3"}.issubset(set(row["node_ids"])) for row in plan["rows"])
    assert not any({"S1", "S2"}.issubset(set(row["node_ids"])) for row in plan["rows"])
    assert {row["artifact_family"] for row in plan["rows"]} >= {"analysis", "configuration"}


def test_up_row_enricher_fills_fields_without_changing_node_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    candidates = [
        _candidate("Анализ влажности почвы", group="Фермерство", bloom="analyze", decision="accepted"),
        _candidate("Оценка кислотности почвы", group="Фермерство", bloom="analyze", decision="accepted"),
    ]
    for index, candidate in enumerate(candidates, start=1):
        candidate.tmp_id = f"S{index}"
    dag_payload = {
        "order": [{"id": "S1"}, {"id": "S2"}],
        "final_edges": [],
    }

    plan = stage_dag_to_up.run({"role": "Агроном", "seniority": "начинающий"}, candidates, dag_payload)
    row = plan["rows"][0]

    assert row["node_ids"] == ["S1", "S2"]
    assert row["project_name"]
    assert row["project_summary"]
    assert row["storytelling"]
    assert "Критерии проверки" in row["materials"]
    assert row["validation_criteria"]


def test_storage_loads_db_backed_artifact_templates() -> None:
    db_path = _runtime_db_path("artifact-template")
    conn = _create_base_catalog_db(db_path)
    try:
        template_id = storage.upsert_curriculum_artifact_template(
            conn,
            code="soil-analysis",
            title="Аналитический проект по теме {theme}",
            artifact_family="analysis",
            artifact_description="Отчёт по теме {theme}: {skills}",
            materials_pattern="Материалы по теме {theme}",
            storytelling_pattern="Кейс по теме {theme}",
            validation_criteria="Проверить выводы по навыкам: {skills}",
            scopes=[{"scope_type": "coverage_area", "scope_name": "Фермерство", "weight": 1.0}],
        )

        templates = storage.load_curriculum_artifact_templates(conn)

        assert template_id > 0
        assert templates[0]["code"] == "soil-analysis"
        assert templates[0]["scopes"][0]["normalized_scope_name"] == "фермерство"
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_up_planner_applies_db_artifact_template_without_changing_node_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    candidates = [
        _candidate("Анализ влажности почвы", group="Фермерство", bloom="analyze", decision="accepted"),
        _candidate("Оценка кислотности почвы", group="Фермерство", bloom="analyze", decision="accepted"),
    ]
    for index, candidate in enumerate(candidates, start=1):
        candidate.tmp_id = f"S{index}"
    dag_payload = {
        "order": [{"id": "S1"}, {"id": "S2"}],
        "final_edges": [],
    }
    spec = {
        "role": "Агроном",
        "seniority": "начинающий",
        "artifact_templates": [
            {
                "id": 1,
                "code": "soil-analysis",
                "title": "Аналитический проект: {theme}",
                "artifact_family": "analysis",
                "artifact_description": "Методологический отчёт по теме {theme}: {skills}",
                "materials_pattern": "Набор данных и чек-лист для темы {theme}",
                "storytelling_pattern": "Участник решает кейс в теме {theme}",
                "validation_criteria": "Проверить аргументацию и навыки: {skills}",
                "priority": 1,
                "scopes": [{"scope_type": "coverage_area", "scope_name": "Фермерство", "normalized_scope_name": "фермерство", "weight": 1.0}],
            }
        ],
    }

    plan = stage_dag_to_up.run(spec, candidates, dag_payload)
    row = plan["rows"][0]

    assert row["node_ids"] == ["S1", "S2"]
    assert row["artifact_template_code"] == "soil-analysis"
    assert row["project_name"] == "Аналитический проект: Фермерство"
    assert "Методологический отчёт" in row["artifact"]
    assert "Набор данных" in row["materials"]
    assert "Проверить аргументацию" in row["validation_criteria"]
    assert plan["report"]["quality_metrics"]["db_template_project_count"] == 1


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


def test_up_planner_scales_hours_to_brief_workload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "UP_MAX_SKILLS_PER_PROJECT", 4)
    candidates = [
        _candidate("Проведение интервью", group="Исследование клиентов", bloom="apply", decision="accepted"),
        _candidate("Формулирование гипотез", group="Продуктовая стратегия", bloom="apply", decision="accepted"),
        _candidate("Разработка MVP", group="Разработка продукта", bloom="apply", decision="accepted"),
    ]
    for index, candidate in enumerate(candidates, start=1):
        candidate.tmp_id = f"S{index}"
    dag_payload = {
        "order": [{"id": f"S{index}"} for index in range(1, 4)],
        "final_edges": [],
    }

    plan = stage_dag_to_up.run({"role": "tester", "seniority": "junior", "target_total_hours": 480}, candidates, dag_payload)

    assert plan["status"] == "built"
    assert plan["summary"]["total_hours"] == 480
    assert all(row["weighted_skills"] for row in plan["rows"])


def test_load_brief_spec_for_plan_restores_workload_from_raw_brief() -> None:
    db_path = _runtime_db_path("brief-spec-workload")
    conn = _create_base_catalog_db(db_path)
    try:
        brief_id = storage.save_brief(
            conn,
            "Программа длится 5-6 месяцев, нагрузка 20 часов в неделю.",
            {"role": "роль", "seniority": "junior", "domain": "домен"},
        )

        spec = load_brief_spec_for_plan(conn, brief_id)

        assert spec["target_total_hours"] == 478
        assert spec["hours_per_week"] == 20.0
        assert "artifact_templates" in spec
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_template_proposals_generate_and_accept() -> None:
    db_path = _runtime_db_path("template-proposals")
    conn = _create_base_catalog_db(db_path)
    try:
        brief_id = storage.save_brief(
            conn,
            "Бриф про customer discovery и MVP.",
            {"role": "основатель", "seniority": "junior", "domain": "стартап"},
        )
        candidates = [
            _candidate("Проводит интервью с пользователями", group="Исследование", decision="accepted"),
            _candidate("Формулирует инсайты проблемы", group="Исследование", decision="accepted"),
        ]
        for candidate in candidates:
            candidate.coverage_area = "выявление проблемы и понимание клиента"
        storage.save_suggestions(conn, brief_id, candidates, {})

        proposals = storage.generate_curriculum_artifact_template_proposals(conn, brief_id=brief_id, plan_id=None)

        assert proposals
        proposal = proposals[0]
        assert proposal["status"] == "open"
        assert "Проводит интервью с пользователями" in proposal["covered_skill_names"]
        assert proposal["scope_names"] == ["выявление проблемы и понимание клиента"]

        accepted = storage.accept_curriculum_artifact_template_proposal(conn, int(proposal["id"]))
        templates = storage.load_curriculum_artifact_templates(conn)

        assert accepted["status"] == "accepted"
        assert templates
        assert templates[0]["scopes"][0]["scope_name"] == "выявление проблемы и понимание клиента"
    finally:
        conn.close()
        db_path.unlink(missing_ok=True)


def test_template_consilium_filters_unknown_scope_and_skill_ids() -> None:
    scope_groups = [
        {
            "scope_name": "исследование пользователя",
            "skills": [
                {"id": 10, "name": "Проведение интервью", "group_name": "Исследование"},
                {"id": 11, "name": "Формулирование инсайтов", "group_name": "Исследование"},
            ],
        }
    ]
    raw = {
        "proposals": [
            {
                "scope_names": ["несуществующая область"],
                "title": "Плохой шаблон",
                "artifact_family": "analysis",
                "covered_skill_ids": [999],
            },
            {
                "scope_names": ["исследование пользователя"],
                "title": "Отчёт исследования пользователя",
                "artifact_family": "analysis",
                "artifact_description": "Студент предъявляет отчёт с фактами и выводами.",
                "project_name_pattern": "Отчёт исследования",
                "materials_pattern": "Бриф, заметки интервью, список навыков: {skills}.",
                "storytelling_pattern": "Студент действует как исследователь продукта.",
                "validation_criteria": "Есть факты, выводы и связь с навыками.",
                "covered_skill_ids": [10, 999],
                "rationale": "Один проверяемый артефакт на область исследования.",
                "confidence": 0.92,
            },
        ]
    }

    proposals = up_template_consilium.validate_proposals(
        raw,
        scope_groups=scope_groups,
        max_proposals=5,
        source="test_consilium",
    )

    assert len(proposals) == 1
    assert proposals[0]["scope_names"] == ["исследование пользователя"]
    assert proposals[0]["covered_skill_ids"] == [10]
    assert proposals[0]["source"] == "test_consilium"


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


def test_spiral_zun_reveals_skills_on_later_touch() -> None:
    node = PlanNode(
        tmp_id="S1",
        name="A core",
        group="theme",
        block_key="theme",
        bloom=5,
        outcomes_know=("Знает основу A",),
        outcomes_can=("Умеет применять A",),
        outcomes_skills=("Владеет A в итоговом артефакте",),
        tools=(),
    )
    primary_project = ProjectBlueprint(
        occurrences=[SkillOccurrence(node=node, role="primary", touch_index=1, bloom_bucket="can")],
        block_key="theme",
        artifact="intro artifact",
    )
    assessment_project = ProjectBlueprint(
        occurrences=[SkillOccurrence(node=node, role="assessment", touch_index=2, bloom_bucket="skills")],
        block_key="theme",
        artifact="assessment artifact",
    )
    occurrence_totals = {"S1": 2}

    primary_outcomes = stage_dag_to_up._project_outcomes(primary_project, occurrence_totals)
    assessment_outcomes = stage_dag_to_up._project_outcomes(assessment_project, occurrence_totals)

    assert "Владеет A" not in primary_outcomes[2]
    assert "Владеет A" in assessment_outcomes[2]


def test_intro_bloom_create_is_clamped_without_explicit_signal() -> None:
    seniority = "\u043d\u0430\u0447\u0438\u043d\u0430\u044e\u0449\u0438\u0439"
    routine = "\u0424\u043e\u0440\u043c\u0443\u043b\u0438\u0440\u0443\u0435\u0442 A/B \u0433\u0438\u043f\u043e\u0442\u0435\u0437\u044b"
    explicit = "\u0421\u043e\u0437\u0434\u0430\u0451\u0442 \u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u0430"

    assert stage_brief_to_catalog.normalize_bloom("create", {"seniority": seniority}, routine) == "analyze"
    assert stage_brief_to_catalog.normalize_bloom("create", {"seniority": seniority}, explicit) == "create"


def test_practical_actions_are_not_downgraded_to_understand() -> None:
    spec = {"seniority": "начинающий"}

    assert stage_brief_to_catalog.normalize_bloom("understand", spec, "Настраивает мониторинг и логирование") == "apply"
    assert stage_brief_to_catalog.normalize_bloom("understand", spec, "Проектирует высокоуровневую архитектуру") == "analyze"


def test_triage_does_not_mark_matched_skill_as_novel() -> None:
    candidate = _candidate("Existing skill", decision="needs_review")
    candidate.resolution = "matched"
    candidate.match_score = 100.0
    candidate.canonical_name = "Existing skill"
    candidate.canonical_group = "Research"
    candidate.confidence = 0.98
    candidate.evidence_ids = []

    stage_brief_to_catalog.triage_candidates([candidate], {"artifact_type": "program_brief"})

    assert "novel_skill" not in candidate.reasons
    assert "single_source" not in candidate.reasons


def test_triage_sends_suspicious_catalog_match_to_review() -> None:
    candidate = _candidate("Проведение customer discovery", group="Исследование клиентов", decision="needs_review")
    candidate.coverage_area = "Исследование клиентов и problem framing"
    candidate.resolution = "alias"
    candidate.match_score = 100.0
    candidate.canonical_name = "Проведение code review"
    candidate.canonical_group = "Инженерная дисциплина"
    candidate.confidence = 0.98
    candidate.council_agreement = 1.0

    stage_brief_to_catalog.triage_candidates([candidate], {"artifact_type": "program_brief"})

    assert candidate.decision == "needs_review"
    assert "catalog_match_suspicious" in candidate.reasons


def test_triage_blocks_low_score_and_generic_catalog_matches() -> None:
    low_score = _candidate("Планирование полива", group="Фермерство", decision="needs_review")
    low_score.resolution = "matched"
    low_score.match_score = 84.0
    low_score.canonical_name = "Планирование полива"
    low_score.canonical_group = "Фермерство"
    low_score.confidence = 0.99
    low_score.council_agreement = 1.0

    generic_group = _candidate("Оценка кислотности почвы", group="Фермерство", decision="needs_review")
    generic_group.resolution = "alias"
    generic_group.match_score = 100.0
    generic_group.canonical_name = "Оценка кислотности почвы"
    generic_group.canonical_group = "Прочие навыки"
    generic_group.confidence = 0.99
    generic_group.council_agreement = 1.0

    stage_brief_to_catalog.triage_candidates([low_score, generic_group], {"artifact_type": "program_brief"})

    assert low_score.decision == "needs_review"
    assert generic_group.decision == "needs_review"
    assert "catalog_match_suspicious" in low_score.reasons
    assert "catalog_match_suspicious" in generic_group.reasons


def test_operational_dag_ignores_edges_that_need_review() -> None:
    candidates = [
        _candidate("A base", bloom="analyze", decision="accepted"),
        _candidate("B target", bloom="apply", decision="accepted"),
    ]
    candidates[0].tmp_id = "A"
    candidates[1].tmp_id = "B"
    edges = [
        stage_catalog_to_dag.PrereqEdge(src="A", dst="B", relation_type="soft", confidence=0.95, source="ai"),
    ]

    stage_catalog_to_dag.triage_edges(edges, candidates)
    accepted_edges = stage_catalog_to_dag.operational_edges(edges)
    dag, _removed_cycle, _removed_transitive = stage_catalog_to_dag.build_dag(accepted_edges, candidates)

    assert edges[0].decision == "needs_review"
    assert accepted_edges == []
    assert dag.number_of_edges() == 0


def test_visual_dag_preview_shows_safe_ai_edges_without_operational_promotion() -> None:
    candidates = [
        _candidate("A base", bloom="apply", decision="accepted"),
        _candidate("B target", bloom="analyze", decision="accepted"),
    ]
    candidates[0].tmp_id = "A"
    candidates[1].tmp_id = "B"
    edges = [
        stage_catalog_to_dag.PrereqEdge(src="A", dst="B", relation_type="soft", confidence=0.95, source="ai"),
    ]

    stage_catalog_to_dag.triage_edges(edges, candidates)
    accepted_edges = stage_catalog_to_dag.operational_edges(edges)
    dag, removed_cycle, removed_transitive = stage_catalog_to_dag.build_dag(accepted_edges, candidates)
    payload = stage_catalog_to_dag.build_dag_payload(edges, dag, removed_cycle, removed_transitive, candidates)
    payload["accepted_edge_count"] = len(accepted_edges)
    stage_catalog_to_dag.add_visual_preview_payload(payload, edges, candidates)

    assert edges[0].decision == "needs_review"
    assert payload["final_edges"] == []
    assert len(payload["visual_edges"]) == 1
    assert payload["visual_edges"][0]["decision"] == "needs_review"
    assert payload["preview_edge_count"] == 1
    assert len(payload["visual_waves"]) == 2


def test_edge_decision_override_promotes_ai_edge_to_operational_dag() -> None:
    candidates = [
        _candidate("A base", bloom="apply", decision="accepted"),
        _candidate("B target", bloom="analyze", decision="accepted"),
    ]
    candidates[0].tmp_id = "A"
    candidates[1].tmp_id = "B"
    edges = [
        stage_catalog_to_dag.PrereqEdge(src="A", dst="B", relation_type="soft", confidence=0.95, source="ai"),
    ]

    stage_catalog_to_dag.triage_edges(edges, candidates)
    stage_catalog_to_dag.apply_edge_decision_overrides(edges, {"A->B": "accepted"})
    accepted_edges = stage_catalog_to_dag.operational_edges(edges)
    dag, _removed_cycle, _removed_transitive = stage_catalog_to_dag.build_dag(accepted_edges, candidates)

    assert edges[0].decision == "accept"
    assert edges[0].reasons == ["human_accepted"]
    assert len(accepted_edges) == 1
    assert dag.number_of_edges() == 1


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


def test_curriculum_csv_writes_a_to_v_with_working_methodology_fields() -> None:
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
                "completion_percent": 50,
                "p2p_checks": 2,
                "skills_list": "Навык A, Навык B",
                "platform_project_name": "platform name",
                "artifact_links": "gitlab link",
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
    assert rows[2][14:] == [
        "99",
        "99",
        "999",
        "50",
        "2",
        "Навык A: 50%, Навык B: 50%",
        "platform name",
        "gitlab link",
    ]


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
