"""Стадия 4: accepted skills + DAG -> spiral curriculum plan draft.

The stage does not generate new skills and does not mutate the DAG. It builds a
deterministic curriculum layer above the DAG: accepted skills become project
occurrences, core threads can reappear later for reinforcement/assessment, and
the final payload remains compatible with the existing UI and CSV export.
"""
from __future__ import annotations

from math import isfinite
import re

from . import config
from . import language
from .curriculum import CurriculumBlock, PlanNode, ProjectBlueprint, SkillOccurrence, build_curriculum_blocks
from .models import SkillCandidate

CSV_PRIMARY_HEADER = [
    "Тематический блок",
    "Цели блока",
    "№",
    "Название контентной единицы (проекта)",
    "Краткое описание",
    "Обр. результаты — что узнает (ЗНАТЬ)",
    "Обр. результаты — что умеет (УМЕТЬ)",
    "Обр. результаты — какой навык (НАВЫКИ)",
    "Необходимое ПО",
    "Доп. материалы для генерации",
    "Сторителлинг",
    "Формат",
    "Кол-во в группе",
    "Трудоёмкость, астр. часы",
    "Трудоёмкость, дни",
    "Общая трудоёмкость, дни",
    "XP за проект",
    "% прохождения проекта",
    "Количество p2p проверок",
    "Список навыков (развесовка)",
    "Название на платформе / Gitlab",
    "Ссылки на GitLab",
]

CSV_SECONDARY_HEADER = [""] * len(CSV_PRIMARY_HEADER)


def _display_name(candidate: SkillCandidate) -> str:
    # Для сматченных сущностей используем каноническое имя каталога.
    if candidate.canonical_name and candidate.resolution in {"matched", "alias", "fuzzy"}:
        return language.localize_skill_label(candidate.canonical_name)
    return language.localize_skill_label(candidate.name)


def _display_group(candidate: SkillCandidate) -> str:
    # Coverage area лучше подходит для тематического блока, чем сырая skill-group.
    value = candidate.coverage_area or candidate.canonical_group or candidate.group or "Общее"
    return language.localize_area_label(value) or language.localize_group_label(value) or "Общее"


def _node_from_candidate(candidate: SkillCandidate) -> PlanNode:
    # Для ЗУН раскладываем индикаторы строго по Bloom-бакетам F/G/H.
    outcomes_know: list[str] = []
    outcomes_can: list[str] = []
    outcomes_skills: list[str] = []
    for indicator in candidate.indicators:
        text = indicator.text.strip()
        if not text:
            continue
        if indicator.bloom in config.UP_BLOOM_KNOW:
            outcomes_know.append(text)
        elif indicator.bloom in config.UP_BLOOM_CAN:
            outcomes_can.append(text)
        else:
            outcomes_skills.append(text)
    if not outcomes_know and not outcomes_can and not outcomes_skills:
        if candidate.bloom <= 2:
            outcomes_know.append(_display_name(candidate))
        elif candidate.bloom <= 4:
            outcomes_can.append(_display_name(candidate))
        else:
            outcomes_skills.append(_display_name(candidate))
    tools = tuple(sorted({tool.strip() for tool in candidate.tools if tool.strip()}))
    return PlanNode(
        tmp_id=candidate.tmp_id,
        name=_display_name(candidate),
        group=candidate.canonical_group or candidate.group or "Без группы",
        block_key=_display_group(candidate),
        bloom=candidate.bloom,
        outcomes_know=tuple(dict.fromkeys(outcomes_know)),
        outcomes_can=tuple(dict.fromkeys(outcomes_can)),
        outcomes_skills=tuple(dict.fromkeys(outcomes_skills)),
        tools=tools,
    )


def _audience_label(spec: dict[str, object] | None) -> str:
    seniority = str((spec or {}).get("seniority") or "").casefold()
    mapping = {
        "junior": "Начальный",
        "junior+": "Начальный",
        "middle": "Средний",
        "senior": "Продвинутый",
        "lead": "Продвинутый",
        "начинающий": "Начальный",
        "базовый": "Начальный",
    }
    return mapping.get(seniority, "Начальный")


def _snap_hours(raw_hours: float) -> int:
    return min(config.UP_HOUR_BANDS, key=lambda band: abs(band - raw_hours))


def _estimate_project_hours(nodes: list[PlanNode]) -> int:
    # В baseline считаем часы от числа навыков и верхнего Bloom в проекте.
    max_bloom = max((node.bloom for node in nodes), default=2)
    raw_hours = 6 + 3 * len(nodes) + 2 * max(0, max_bloom - 2)
    return _snap_hours(raw_hours)


def _compact_label(value: str, *, max_words: int = 5, max_chars: int = 56) -> str:
    """Return a short Russian UI/CSV label without losing technical terms."""
    text = language.localize_area_label(value) or language.localize_skill_label(value) or value
    text = re.sub(r"\s+", " ", text.replace("—", "-")).strip(" .,-")
    if not text:
        return "Общее"
    # Long clarifications after colon are useful in coverage audit, but too noisy as block titles.
    text = text.split(":", 1)[0].strip()
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words])
    if len(text) > max_chars:
        text = text[:max_chars].rstrip(" ,.-") + "..."
    return text or "Общее"


def _join_limited(values: list[str], *, limit: int = 3) -> str:
    labels = [_compact_label(value, max_words=5, max_chars=48) for value in values if value]
    unique = list(dict.fromkeys(labels))
    if len(unique) <= limit:
        return ", ".join(unique)
    return ", ".join(unique[:limit]) + f" и ещё {len(unique) - limit}"


def _block_title(block_index: int, block_keys: list[str]) -> str:
    theme = _join_limited(block_keys, limit=2) or "Общее"
    return f"Блок {block_index}. {theme}"


def _block_goal(nodes: list[PlanNode]) -> str:
    names = _join_limited([node.name for node in nodes], limit=4)
    return f"Сформировать практику: {names}" if names else "Сформировать практический результат блока."


def _project_name(nodes: list[PlanNode], block_index: int, project_index: int) -> str:
    # Пока делаем детерминированный title; LLM-enrichment можно добавить отдельным нижним генератором.
    if len(nodes) == 1:
        return _compact_label(nodes[0].name, max_words=6, max_chars=64)
    anchor = nodes[-1].name
    return _compact_label(anchor, max_words=6, max_chars=64)


def _project_summary(nodes: list[PlanNode], role: str) -> str:
    names = ", ".join(node.name for node in nodes)
    return (
        f"Практический проект, в котором участник в роли «{role}» применяет навыки {names} "
        "и собирает проверяемый промежуточный результат."
    )


def _project_storytelling(nodes: list[PlanNode], role: str, block_key: str) -> str:
    names = ", ".join(node.name for node in nodes)
    return (
        f"Ты работаешь как {role} и решаешь учебный кейс по теме «{block_key}». "
        f"Нужно применить навыки {names} в ограниченном прикладном сценарии и защитить результат."
    )


def _occurrence_outcome_sources(occurrence: SkillOccurrence) -> tuple[str, tuple[str, ...]]:
    node = occurrence.node
    if occurrence.bloom_bucket == "know":
        return "know", node.outcomes_know or (f"Объясняет назначение навыка «{node.name}» в рабочем контексте.",)
    if occurrence.bloom_bucket == "skills":
        return "skills", node.outcomes_skills or node.outcomes_can or (f"Интегрирует навык «{node.name}» в проверяемый артефакт.",)
    if occurrence.role in {"assessment", "reinforcement"} and occurrence.touch_index >= 3:
        return "skills", node.outcomes_skills or node.outcomes_can or (f"Закрепляет навык «{node.name}» в новом проектном контексте.",)
    return "can", node.outcomes_can or node.outcomes_know or (f"Применяет навык «{node.name}» для решения проектной задачи.",)


def _fallback_outcome(occurrence: SkillOccurrence) -> tuple[str, str]:
    node = occurrence.node
    if occurrence.role == "assessment":
        return "skills", f"Защищает результат, демонстрируя владение навыком «{node.name}»."
    if occurrence.role == "reinforcement":
        return "can", f"Повторно применяет навык «{node.name}» в более сложном сценарии."
    if node.bloom <= 2:
        return "know", f"Понимает ключевые принципы навыка «{node.name}»."
    if node.bloom <= 4:
        return "can", f"Применяет навык «{node.name}» в практическом задании."
    return "skills", f"Создаёт или оценивает артефакт с использованием навыка «{node.name}»."


def _project_outcomes(project: ProjectBlueprint) -> tuple[str, str, str, int]:
    buckets: dict[str, list[str]] = {"know": [], "can": [], "skills": []}
    max_outcomes = max(1, int(config.UP_TARGET_OUTCOMES_MAX))
    min_outcomes = max(1, min(int(config.UP_TARGET_OUTCOMES_MIN), max_outcomes))

    for occurrence in project.occurrences:
        bucket, outcomes = _occurrence_outcome_sources(occurrence)
        for outcome in outcomes:
            text = outcome.strip()
            if text and text not in buckets[bucket] and sum(len(items) for items in buckets.values()) < max_outcomes:
                buckets[bucket].append(text)

    for occurrence in project.occurrences:
        if sum(len(items) for items in buckets.values()) >= min_outcomes:
            break
        bucket, outcome = _fallback_outcome(occurrence)
        if outcome not in buckets[bucket]:
            buckets[bucket].append(outcome)

    # Even a deliberately small introductory project should expose a complete
    # ZUN profile instead of a single "demonstrates skill" line.
    anchor = project.unique_nodes[-1].name if project.unique_nodes else "проектный навык"
    completion_fallbacks = [
        ("know", f"Описывает контекст применения навыка «{anchor}»."),
        ("can", f"Применяет навык «{anchor}» при создании проектного артефакта."),
        ("skills", f"Оформляет и защищает проверяемый результат по теме «{project.block_key}»."),
    ]
    for bucket, outcome in completion_fallbacks:
        if sum(len(items) for items in buckets.values()) >= min_outcomes:
            break
        if outcome not in buckets[bucket]:
            buckets[bucket].append(outcome)

    return (
        "\n".join(buckets["know"]),
        "\n".join(buckets["can"]),
        "\n".join(buckets["skills"]),
        sum(len(items) for items in buckets.values()),
    )


def _project_skill_list(project: ProjectBlueprint) -> str:
    labels: list[str] = []
    for occurrence in project.occurrences:
        suffix = ""
        if occurrence.role == "reinforcement":
            suffix = " (закрепление)"
        elif occurrence.role == "assessment":
            suffix = " (контроль/владение)"
        label = occurrence.node.name + suffix
        if label not in labels:
            labels.append(label)
    return ", ".join(labels)


def _default_group_size(delivery_format: str) -> int:
    bounds = config.UP_FORMAT_GROUP_SIZES.get(delivery_format, (1, 1))
    return int(bounds[0])


def _format_rows(blocks: list[CurriculumBlock], spec: dict[str, object] | None) -> list[dict[str, object]]:
    role = str((spec or {}).get("role") or "участник программы").strip()
    rows: list[dict[str, object]] = []
    row_number = 0
    for block_index, block in enumerate(blocks, start=1):
        all_block_nodes = [node for project in block.projects for node in project.unique_nodes]
        block_keys = sorted({node.block_key for node in all_block_nodes})
        block_title = _block_title(block_index, block_keys)
        block_goal = _block_goal(all_block_nodes)
        for project_index, project in enumerate(block.projects, start=1):
            row_number += 1
            project_nodes = project.unique_nodes
            effort_hours = _estimate_project_hours(project_nodes)
            required_tools = ", ".join(sorted({tool for node in project_nodes for tool in node.tools}))
            outcomes_know, outcomes_can, outcomes_skills, outcome_count = _project_outcomes(project)
            project_name = _project_name(project_nodes, block_index, project_index)
            block_key = project.block_key or (project_nodes[0].block_key if project_nodes else "Общее")
            delivery_format = config.UP_DEFAULT_FORMAT
            rows.append(
                {
                    "block_index": block_index,
                    "row_number": row_number,
                    "project_index_in_block": project_index,
                    "block_title": block_title if project_index == 1 else "",
                    "block_goal": block_goal,
                    "project_name": project_name,
                    "project_summary": _project_summary(project_nodes, role),
                    "outcomes_know": outcomes_know,
                    "outcomes_can": outcomes_can,
                    "outcomes_skills": outcomes_skills,
                    "learning_outcomes": "\n".join(item for item in [outcomes_know, outcomes_can, outcomes_skills] if item),
                    "skills_list": _project_skill_list(project),
                    "node_ids": project.node_ids,
                    "node_names": [node.name for node in project_nodes],
                    "occurrence_count": len(project.occurrences),
                    "outcome_count": outcome_count,
                    "artifact": project.artifact,
                    "audience_level": _audience_label(spec),
                    "required_tools": required_tools,
                    "materials": "",
                    "storytelling": _project_storytelling(project_nodes, role, block_key),
                    "delivery_format": delivery_format,
                    "group_size": _default_group_size(delivery_format),
                    "effort_hours": effort_hours,
                    "effort_days": "",
                    "cumulative_days": "",
                    "xp": "",
                    "completion_percent": "",
                    "p2p_checks": "",
                    "weighted_skills": "",
                    "platform_project_name": "",
                    "artifact_links": "",
                }
            )
    return rows


def _plan_report(rows: list[dict[str, object]], dag_payload: dict[str, object]) -> dict[str, object]:
    # Проверяем порядок по стабильным tmp_id, а не по отображаемым именам.
    position_by_node: dict[str, tuple[int, int]] = {}
    row_by_node: dict[str, int] = {}
    for row in rows:
        row_number = int(row.get("row_number", 0) or 0)
        node_ids = row.get("node_ids") if isinstance(row.get("node_ids"), list) else []
        for skill_index, node_id in enumerate(node_ids):
            node_key = str(node_id)
            position_by_node.setdefault(node_key, (row_number, skill_index))
            row_by_node.setdefault(node_key, row_number)
    broken_order: list[str] = []
    project_violations: list[str] = []
    for edge in dag_payload.get("final_edges", []):
        if not isinstance(edge, dict):
            continue
        src_id = str(edge.get("src_id") or "")
        dst_id = str(edge.get("dst_id") or "")
        src = str(edge.get("src") or src_id)
        dst = str(edge.get("dst") or dst_id)
        if not src_id or not dst_id or src_id not in position_by_node or dst_id not in position_by_node:
            continue
        if row_by_node[src_id] == row_by_node[dst_id]:
            if str(edge.get("relation_type") or "").casefold() == "hard":
                project_violations.append(f"{src} -> {dst}")
            continue
        if position_by_node[src_id] >= position_by_node[dst_id]:
            broken_order.append(f"{src} -> {dst}")
    return {
        "coverage_ok": not broken_order and not project_violations,
        "order_violations": broken_order,
        "project_violations": project_violations,
    }


def _quality_metrics(rows: list[dict[str, object]], planner_meta: dict[str, object]) -> dict[str, object]:
    project_count = len(rows)
    if not project_count:
        return {
            "avg_skills_per_project": 0.0,
            "avg_outcomes_per_project": 0.0,
            "single_skill_project_count": 0,
            "overloaded_project_count": 0,
            "core_thread_count": 0,
            "repeated_thread_count": 0,
            "spiral_enabled": bool(config.UP_SPIRAL_ENABLED),
            "target_skills_per_project": [config.UP_TARGET_SKILLS_MIN, config.UP_TARGET_SKILLS_MAX],
            "target_outcomes_per_project": [config.UP_TARGET_OUTCOMES_MIN, config.UP_TARGET_OUTCOMES_MAX],
        }
    skill_counts = [len(row.get("node_ids") or []) for row in rows]
    outcome_counts = [int(row.get("outcome_count", 0) or 0) for row in rows]
    overloaded = [
        row
        for row in rows
        if len(row.get("node_ids") or []) > config.UP_TARGET_SKILLS_MAX
        or int(row.get("outcome_count", 0) or 0) > config.UP_TARGET_OUTCOMES_MAX
    ]
    return {
        "avg_skills_per_project": round(sum(skill_counts) / project_count, 2),
        "avg_outcomes_per_project": round(sum(outcome_counts) / project_count, 2),
        "single_skill_project_count": sum(1 for count in skill_counts if count <= 1),
        "overloaded_project_count": len(overloaded),
        "core_thread_count": len(planner_meta.get("core_thread_ids") or []),
        "repeated_thread_count": int(planner_meta.get("repeated_thread_count", 0) or 0),
        "spiral_enabled": bool(config.UP_SPIRAL_ENABLED),
        "target_skills_per_project": [config.UP_TARGET_SKILLS_MIN, config.UP_TARGET_SKILLS_MAX],
        "target_outcomes_per_project": [config.UP_TARGET_OUTCOMES_MIN, config.UP_TARGET_OUTCOMES_MAX],
    }


def run(spec: dict[str, object] | None, candidates: list[SkillCandidate], dag_payload: dict[str, object]) -> dict[str, object]:
    # Планировщик работает только по фактически принятым узлам DAG.
    if not candidates or not dag_payload.get("order"):
        return {
            "status": "deferred",
            "message": "Черновик УП пока не строится: нет принятых навыков с валидным DAG.",
            "title": "Черновик учебного плана",
            "audience_level": _audience_label(spec),
            "source_policy": "accepted_only",
            "summary": {"blocks": 0, "projects": 0, "total_hours": 0, "total_days": 0, "total_xp": 0},
            "rows": [],
            "blocks": [],
            "csv_primary_header": CSV_PRIMARY_HEADER,
            "csv_secondary_header": CSV_SECONDARY_HEADER,
            "report": {"coverage_ok": False, "order_violations": [], "project_violations": [], "quality_metrics": _quality_metrics([], {})},
        }

    nodes = [_node_from_candidate(candidate) for candidate in candidates]
    blocks, planner_meta = build_curriculum_blocks(nodes, dag_payload)
    rows = _format_rows(blocks, spec)
    total_hours = sum(float(row.get("effort_hours", 0) or 0) for row in rows)
    total_days = sum(float(row.get("effort_days", 0) or 0) for row in rows)
    total_xp = sum(int(row.get("xp", 0) or 0) for row in rows)
    report = _plan_report(rows, dag_payload)
    report["quality_metrics"] = _quality_metrics(rows, planner_meta)
    report["planner_meta"] = planner_meta
    is_invalid = bool(report["order_violations"] or report.get("project_violations"))

    # Для UI держим и блочное представление, и плоские CSV-совместимые строки.
    block_payloads: list[dict[str, object]] = []
    rows_by_block: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        rows_by_block.setdefault(int(row["block_index"]), []).append(row)
    for block_index, block_rows in rows_by_block.items():
        total_block_hours = sum(float(row.get("effort_hours", 0) or 0) for row in block_rows)
        total_block_days = sum(float(row.get("effort_days", 0) or 0) for row in block_rows)
        block_payloads.append(
            {
                "block_index": block_index,
                "title": str(block_rows[0].get("block_title") or f"Блок {block_index}"),
                "goal": str(block_rows[0].get("block_goal") or ""),
                "project_count": len(block_rows),
                "total_hours": total_block_hours,
                "total_days": round(total_block_days, 2),
                "rows": block_rows,
            }
        )

    return {
        "status": "invalid" if is_invalid else "built",
        "message": (
            "Черновик УП невалиден: найдены нарушения порядка DAG. Нужна перенарезка проектов или правка DAG."
            if is_invalid
            else "Черновик УП построен детерминированно по принятым skills и текущему DAG."
        ),
        "title": "Черновик учебного плана",
        "audience_level": _audience_label(spec),
        "source_policy": "accepted_only",
        "planner_meta": planner_meta,
        "summary": {
            "blocks": len(block_payloads),
            "projects": len(rows),
            "total_hours": int(total_hours) if isfinite(total_hours) else 0,
            "total_days": round(total_days, 2) if isfinite(total_days) else 0.0,
            "total_xp": int(total_xp),
            "avg_skills_per_project": report["quality_metrics"]["avg_skills_per_project"],
            "avg_outcomes_per_project": report["quality_metrics"]["avg_outcomes_per_project"],
            "repeated_thread_count": report["quality_metrics"]["repeated_thread_count"],
        },
        "rows": rows,
        "blocks": block_payloads,
        "csv_primary_header": CSV_PRIMARY_HEADER,
        "csv_secondary_header": CSV_SECONDARY_HEADER,
        "report": report,
    }
