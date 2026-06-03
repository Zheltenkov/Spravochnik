"""Стадия 4: accepted skills + DAG -> черновик учебного плана.

Делаем детерминированный upper planner:
- не генерируем новые skills;
- не меняем DAG;
- только упаковываем принятые узлы в блоки/проекты и рендерим строки УП.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import networkx as nx

from . import config
from .models import SkillCandidate

CSV_PRIMARY_HEADER = [
    "Название всего блока (если делим на блоки)",
    "Цели блока",
    "№",
    "Название проекта",
    "Краткое описание проекта",
    "Образовательные результаты (знает, понимает, умеет)",
    "Список навыков",
    "Уровень аудитории",
    "Обязательные инструменты (через запятую)",
    "Сторителлинг",
    "Формат",
    "Кол-во в группе",
    "Трудоемкость, астр.часы",
    "Трудоемкость, дни",
    "Общая трудоемкость, дни",
    "XP за проект",
    "Название проекта на платформе и в Gitlab",
    "Ссылки на GitLab/Google docs",
]

CSV_SECONDARY_HEADER = [
    "Название всего блока (если делим на блоки)",
    "- цели всего блока (чему научим)",
    "",
    "Название проекта",
    "Краткое описание, что представляет собой проект и задания в нем",
    "что узнает и чему научится участник конкретно в этом проекте,\nиспользуем таксономию Блума",
    "список навыков, которые осваивает пир в этом проекте",
    "Уровень подготовки участника",
    "Инструменты, обязательные для выполнения проекта",
    "Кейс, роль, рабочая ситуация и ограничения проекта",
    "",
    "",
    "",
    "",
    "",
    "",
    "очень важно придерживаться правил именования проектов (конфля)",
    "ссылки на готовые проекты - идеально ссылки на репы в гитлабе",
]


@dataclass(frozen=True)
class PlanNode:
    """Нормализованный узел для upper planner."""

    tmp_id: str
    name: str
    group: str
    block_key: str
    bloom: int
    outcomes: tuple[str, ...]
    tools: tuple[str, ...]


def _display_name(candidate: SkillCandidate) -> str:
    # Для сматченных сущностей используем каноническое имя каталога.
    if candidate.canonical_name and candidate.resolution in {"matched", "alias", "fuzzy"}:
        return candidate.canonical_name
    return candidate.name


def _display_group(candidate: SkillCandidate) -> str:
    # Coverage area лучше подходит для тематического блока, чем сырая skill-group.
    return candidate.coverage_area or candidate.canonical_group or candidate.group or "Общее"


def _node_from_candidate(candidate: SkillCandidate) -> PlanNode:
    # Для outcomes берем индикаторы кандидата, а не только name, чтобы Блум не потерялся.
    outcomes = tuple(indicator.text.strip() for indicator in candidate.indicators if indicator.text.strip())
    if not outcomes:
        outcomes = (_display_name(candidate),)
    tools = tuple(sorted({tool.strip() for tool in candidate.tools if tool.strip()}))
    return PlanNode(
        tmp_id=candidate.tmp_id,
        name=_display_name(candidate),
        group=candidate.canonical_group or candidate.group or "Без группы",
        block_key=_display_group(candidate),
        bloom=candidate.bloom,
        outcomes=outcomes,
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


def _slugify(value: str) -> str:
    lowered = value.casefold().replace("ё", "е")
    slug = "-".join(part for part in "".join(ch if ch.isalnum() else "-" for ch in lowered).split("-") if part)
    return slug or "project"


def _project_name(nodes: list[PlanNode], block_index: int, project_index: int) -> str:
    # Пока делаем детерминированный title; LLM-enrichment можно добавить отдельным нижним генератором.
    if len(nodes) == 1:
        return nodes[0].name
    anchor = nodes[-1].name
    return f"{anchor} — проект {block_index}.{project_index}"


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


def _build_block_graph(nodes: list[PlanNode], dag_payload: dict[str, object]) -> tuple[nx.DiGraph, dict[str, int]]:
    # Делаем граф тематических блоков поверх DAG, чтобы сохранять порядок между областями.
    position = {
        str(item.get("id")): index
        for index, item in enumerate(dag_payload.get("order", []))
        if isinstance(item, dict) and item.get("id") is not None
    }
    by_id = {node.tmp_id: node for node in nodes}
    block_graph = nx.DiGraph()
    block_graph.add_nodes_from({node.block_key for node in nodes})
    for edge in dag_payload.get("final_edges", []):
        if not isinstance(edge, dict):
            continue
        src_id = str(edge.get("src_id") or "")
        dst_id = str(edge.get("dst_id") or "")
        if src_id not in by_id or dst_id not in by_id:
            continue
        src_block = by_id[src_id].block_key
        dst_block = by_id[dst_id].block_key
        if src_block != dst_block:
            block_graph.add_edge(src_block, dst_block)
    return block_graph, position


def _ordered_superblocks(nodes: list[PlanNode], dag_payload: dict[str, object]) -> list[tuple[str, ...]]:
    # Схлопываем возможные циклы на уровне тематических блоков и упорядочиваем супер-блоки.
    block_graph, position = _build_block_graph(nodes, dag_payload)
    if block_graph.number_of_nodes() == 0:
        return []
    condensation = nx.condensation(block_graph)

    def _min_pos(component_id: int) -> int:
        members = condensation.nodes[component_id]["members"]
        node_positions = [
            position.get(node.tmp_id, 10**9)
            for node in nodes
            if node.block_key in members
        ]
        return min(node_positions, default=10**9)

    ordered_component_ids = list(nx.lexicographical_topological_sort(condensation, key=_min_pos))
    return [tuple(sorted(condensation.nodes[item]["members"])) for item in ordered_component_ids]


def _pack_projects(nodes: list[PlanNode], dag_payload: dict[str, object]) -> list[list[list[PlanNode]]]:
    # Сначала упаковываем навыки в проекты внутри супер-блока, затем режем блок по лимиту числа проектов.
    position = {
        str(item.get("id")): index
        for index, item in enumerate(dag_payload.get("order", []))
        if isinstance(item, dict) and item.get("id") is not None
    }
    blocks: list[list[list[PlanNode]]] = []
    by_id = {node.tmp_id: node for node in nodes}
    for component_blocks in _ordered_superblocks(nodes, dag_payload):
        component_nodes = sorted(
            [node for node in nodes if node.block_key in component_blocks],
            key=lambda item: position.get(item.tmp_id, 10**9),
        )
        projects_in_component: list[list[PlanNode]] = []
        current: list[PlanNode] = []
        for node in component_nodes:
            candidate_project = current + [node]
            if (
                current
                and len(candidate_project) <= config.UP_MAX_SKILLS_PER_PROJECT
                and _estimate_project_hours(candidate_project) <= max(config.UP_HOUR_BANDS)
            ):
                current = candidate_project
            else:
                if current:
                    projects_in_component.append(current)
                current = [node]
        if current:
            projects_in_component.append(current)
        for offset in range(0, len(projects_in_component), config.UP_MAX_PROJECTS_PER_BLOCK):
            block_projects = projects_in_component[offset : offset + config.UP_MAX_PROJECTS_PER_BLOCK]
            blocks.append(block_projects)
    if not blocks and by_id:
        blocks.append([[node] for node in sorted(nodes, key=lambda item: position.get(item.tmp_id, 10**9))])
    return blocks


def _format_rows(blocks: list[list[list[PlanNode]]], spec: dict[str, object] | None) -> list[dict[str, object]]:
    audience_level = _audience_label(spec)
    role = str((spec or {}).get("role") or "участник программы").strip()
    rows: list[dict[str, object]] = []
    row_number = 0
    cumulative_days = 0.0
    for block_index, block in enumerate(blocks, start=1):
        block_keys = sorted({project[0].block_key for project in block if project})
        block_title = f"Блок {block_index}. " + " / ".join(block_keys)
        all_block_nodes = [node for project in block for node in project]
        block_goal = "Освоить: " + ", ".join(node.name for node in all_block_nodes)
        for project_index, project_nodes in enumerate(block, start=1):
            row_number += 1
            effort_hours = _estimate_project_hours(project_nodes)
            effort_days = round(effort_hours / config.UP_HOURS_PER_DAY, 2) if config.UP_HOURS_PER_DAY else 0.0
            cumulative_days = round(cumulative_days + effort_days, 2)
            required_tools = ", ".join(sorted({tool for node in project_nodes for tool in node.tools}))
            learning_outcomes = "\n".join(
                dict.fromkeys(outcome for node in project_nodes for outcome in node.outcomes)
            )
            project_name = _project_name(project_nodes, block_index, project_index)
            block_key = project_nodes[0].block_key if project_nodes else "Общее"
            rows.append(
                {
                    "block_index": block_index,
                    "row_number": row_number,
                    "project_index_in_block": project_index,
                    "block_title": block_title if project_index == 1 else "",
                    "block_goal": block_goal,
                    "project_name": project_name,
                    "project_summary": _project_summary(project_nodes, role),
                    "learning_outcomes": learning_outcomes,
                    "skills_list": ", ".join(node.name for node in project_nodes),
                    "audience_level": audience_level,
                    "required_tools": required_tools,
                    "storytelling": _project_storytelling(project_nodes, role, block_key),
                    "delivery_format": "индивидуальный",
                    "group_size": "",
                    "effort_hours": effort_hours,
                    "effort_days": effort_days,
                    "cumulative_days": cumulative_days,
                    "xp": effort_hours * config.UP_XP_PER_HOUR,
                    "platform_project_name": f"UP_{block_index}_{project_index}_{_slugify(project_name)}",
                    "artifact_links": "",
                }
            )
    return rows


def _plan_report(rows: list[dict[str, object]], dag_payload: dict[str, object]) -> dict[str, object]:
    # Проверяем, что порядок строк не нарушает DAG-отношения.
    order_by_node: dict[str, int] = {}
    for row in rows:
        skills = str(row.get("skills_list") or "")
        for skill_name in [item.strip() for item in skills.split(",") if item.strip()]:
            order_by_node.setdefault(skill_name, int(row.get("row_number", 0) or 0))
    broken_order: list[str] = []
    for edge in dag_payload.get("final_edges", []):
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("src") or "")
        dst = str(edge.get("dst") or "")
        if src and dst and order_by_node.get(src, 0) >= order_by_node.get(dst, 10**9):
            broken_order.append(f"{src} -> {dst}")
    return {
        "coverage_ok": not broken_order,
        "order_violations": broken_order,
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
            "report": {"coverage_ok": False, "order_violations": []},
        }

    nodes = [_node_from_candidate(candidate) for candidate in candidates]
    blocks = _pack_projects(nodes, dag_payload)
    rows = _format_rows(blocks, spec)
    total_hours = sum(float(row.get("effort_hours", 0) or 0) for row in rows)
    total_days = sum(float(row.get("effort_days", 0) or 0) for row in rows)
    total_xp = sum(int(row.get("xp", 0) or 0) for row in rows)
    report = _plan_report(rows, dag_payload)

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
        "status": "built",
        "message": "Черновик УП построен детерминированно по принятым skills и текущему DAG.",
        "title": "Черновик учебного плана",
        "audience_level": _audience_label(spec),
        "source_policy": "accepted_only",
        "summary": {
            "blocks": len(block_payloads),
            "projects": len(rows),
            "total_hours": int(total_hours) if isfinite(total_hours) else 0,
            "total_days": round(total_days, 2) if isfinite(total_days) else 0.0,
            "total_xp": int(total_xp),
        },
        "rows": rows,
        "blocks": block_payloads,
        "csv_primary_header": CSV_PRIMARY_HEADER,
        "csv_secondary_header": CSV_SECONDARY_HEADER,
        "report": report,
    }
