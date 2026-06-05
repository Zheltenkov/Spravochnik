"""Deterministic spiral curriculum planner.

The planner intentionally does not call LLMs. It transforms accepted skills and
the prerequisite DAG into project blueprints that are denser and more
pedagogically useful than a one-skill-per-project topological walk.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import networkx as nx

from .. import config
from .domain import CurriculumBlock, PlanNode, ProjectBlueprint, SkillOccurrence


def _dag_position(dag_payload: dict[str, object]) -> dict[str, int]:
    return {
        str(item.get("id")): index
        for index, item in enumerate(dag_payload.get("order", []))
        if isinstance(item, dict) and item.get("id") is not None
    }


def _is_reliable_theme_edge(edge: dict[str, object]) -> bool:
    relation_type = str(edge.get("relation_type") or "").casefold()
    if relation_type == "hard":
        return True
    try:
        confidence = float(edge.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return confidence >= config.TAU_EDGE_ACCEPT


def _direct_edge_pairs(dag_payload: dict[str, object], *, hard_only: bool = False) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for edge in dag_payload.get("final_edges", []):
        if not isinstance(edge, dict):
            continue
        if hard_only and str(edge.get("relation_type") or "").casefold() != "hard":
            continue
        src_id = str(edge.get("src_id") or "")
        dst_id = str(edge.get("dst_id") or "")
        if src_id and dst_id:
            pairs.add((src_id, dst_id))
    return pairs


def _has_direct_edge(node: PlanNode, project_nodes: list[PlanNode], direct_edges: set[tuple[str, str]]) -> bool:
    return any(
        (node.tmp_id, existing.tmp_id) in direct_edges or (existing.tmp_id, node.tmp_id) in direct_edges
        for existing in project_nodes
    )


def _build_block_graph(nodes: list[PlanNode], dag_payload: dict[str, object]) -> tuple[nx.DiGraph, dict[str, int]]:
    position = _dag_position(dag_payload)
    by_id = {node.tmp_id: node for node in nodes}
    block_graph = nx.DiGraph()
    block_graph.add_nodes_from({node.block_key for node in nodes})
    for edge in dag_payload.get("final_edges", []):
        if not isinstance(edge, dict) or not _is_reliable_theme_edge(edge):
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
    block_graph, position = _build_block_graph(nodes, dag_payload)
    if block_graph.number_of_nodes() == 0:
        return []
    condensation = nx.condensation(block_graph)

    def _min_pos(component_id: int) -> int:
        members = condensation.nodes[component_id]["members"]
        return min(
            (position.get(node.tmp_id, 10**9) for node in nodes if node.block_key in members),
            default=10**9,
        )

    ordered_component_ids = list(nx.lexicographical_topological_sort(condensation, key=_min_pos))
    superblocks: list[tuple[str, ...]] = []
    max_themes = max(1, int(config.UP_MAX_THEMES_PER_BLOCK))
    for component_id in ordered_component_ids:
        members = list(condensation.nodes[component_id]["members"])
        ordered_members = sorted(
            members,
            key=lambda block_key: min(
                (position.get(node.tmp_id, 10**9) for node in nodes if node.block_key == block_key),
                default=10**9,
            ),
        )
        for offset in range(0, len(ordered_members), max_themes):
            superblocks.append(tuple(ordered_members[offset : offset + max_themes]))
    return superblocks


def _artifact_for(nodes: list[PlanNode], block_key: str) -> str:
    if len(nodes) == 1:
        return f"Проверяемый артефакт по навыку «{nodes[0].name}»"
    anchor = nodes[-1].name
    return f"Интегративный артефакт по теме «{anchor}»"


def _pack_primary_projects(nodes: list[PlanNode], dag_payload: dict[str, object]) -> list[CurriculumBlock]:
    """Pack accepted skills into integrative antichain projects.

    Projects are formed from consecutive nodes in the global DAG order. This is
    stricter than theme-first packing, but it prevents a later skill from being
    pulled too early only because it shares a theme with an earlier one.
    """
    position = _dag_position(dag_payload)
    direct_edges = _direct_edge_pairs(dag_payload, hard_only=True)
    by_id = {node.tmp_id: node for node in nodes}
    max_skills = max(1, int(config.UP_MAX_SKILLS_PER_PROJECT))

    ordered = sorted(nodes, key=lambda item: (position.get(item.tmp_id, 10**9), item.bloom, item.name))
    projects: list[ProjectBlueprint] = []
    current: list[PlanNode] = []
    for node in ordered:
        can_append = current and len(current) < max_skills and not _has_direct_edge(node, current, direct_edges)
        if can_append:
            current.append(node)
            continue
        if current:
            projects.append(
                ProjectBlueprint(
                    occurrences=[SkillOccurrence(item, role="primary", touch_index=1) for item in current],
                    block_key=current[0].block_key,
                    artifact=_artifact_for(current, current[0].block_key),
                )
            )
        current = [node]
    if current:
        projects.append(
            ProjectBlueprint(
                occurrences=[SkillOccurrence(item, role="primary", touch_index=1) for item in current],
                block_key=current[0].block_key,
                artifact=_artifact_for(current, current[0].block_key),
            )
        )
    blocks: list[CurriculumBlock] = []
    if projects:
        for offset in range(0, len(projects), config.UP_MAX_PROJECTS_PER_BLOCK):
            chunk = projects[offset : offset + config.UP_MAX_PROJECTS_PER_BLOCK]
            blocks.append(CurriculumBlock(block_keys=_ordered_block_keys(chunk), projects=chunk))
    elif by_id:
        blocks.append(CurriculumBlock(block_keys=tuple({node.block_key for node in ordered}), projects=[]))
    return blocks


def _flatten_projects(blocks: list[CurriculumBlock]) -> list[ProjectBlueprint]:
    return [project for block in blocks for project in block.projects]


def _project_min_position(project: ProjectBlueprint, position: dict[str, int]) -> int:
    node_ids = [occurrence.node.tmp_id for occurrence in project.primary_occurrences] or project.node_ids
    return min((position.get(node_id, 10**9) for node_id in node_ids), default=10**9)


def _ordered_block_keys(projects: list[ProjectBlueprint]) -> tuple[str, ...]:
    keys: list[str] = []
    for project in projects:
        if project.block_key and project.block_key not in keys:
            keys.append(project.block_key)
    return tuple(keys) or ("Общее",)


def _regroup_projects_by_dag_order(blocks: list[CurriculumBlock], dag_payload: dict[str, object]) -> list[CurriculumBlock]:
    """Preserve global DAG order after thematic project packing.

    Thematic superblocks are useful for packing, but the final curriculum order
    must still respect the accepted DAG topological sequence.
    """

    position = _dag_position(dag_payload)
    projects = sorted(
        _flatten_projects(blocks),
        key=lambda project: (_project_min_position(project, position), project.block_key, project.artifact),
    )
    regrouped: list[CurriculumBlock] = []
    chunk_size = max(1, int(config.UP_MAX_PROJECTS_PER_BLOCK))
    for offset in range(0, len(projects), chunk_size):
        chunk = projects[offset : offset + chunk_size]
        regrouped.append(CurriculumBlock(block_keys=_ordered_block_keys(chunk), projects=chunk))
    return regrouped


def _primary_project_index(projects: list[ProjectBlueprint]) -> dict[str, int]:
    index: dict[str, int] = {}
    for project_index, project in enumerate(projects):
        for occurrence in project.primary_occurrences:
            index.setdefault(occurrence.node.tmp_id, project_index)
    return index


def _centrality_scores(nodes: list[PlanNode], dag_payload: dict[str, object]) -> dict[str, float]:
    by_id = {node.tmp_id: node for node in nodes}
    degree: Counter[str] = Counter()
    reliable_degree: Counter[str] = Counter()
    for edge in dag_payload.get("final_edges", []):
        if not isinstance(edge, dict):
            continue
        src_id = str(edge.get("src_id") or "")
        dst_id = str(edge.get("dst_id") or "")
        if src_id in by_id and dst_id in by_id:
            degree[src_id] += 1
            degree[dst_id] += 1
            if _is_reliable_theme_edge(edge):
                reliable_degree[src_id] += 1
                reliable_degree[dst_id] += 1
    block_frequency = Counter(node.block_key for node in nodes)
    return {
        node.tmp_id: float(reliable_degree[node.tmp_id] * 2 + degree[node.tmp_id] + min(block_frequency[node.block_key], 3) * 0.25)
        for node in nodes
    }


def _select_core_threads(nodes: list[PlanNode], dag_payload: dict[str, object]) -> list[PlanNode]:
    if not config.UP_SPIRAL_ENABLED:
        return []
    scores = _centrality_scores(nodes, dag_payload)
    candidates = [node for node in nodes if scores.get(node.tmp_id, 0.0) > 0.0]
    if len(nodes) >= config.UP_CORE_THREAD_MIN and len(candidates) < config.UP_CORE_THREAD_MIN:
        candidates = nodes[: config.UP_CORE_THREAD_MIN]
    ordered = sorted(candidates, key=lambda node: (-scores.get(node.tmp_id, 0.0), node.bloom, node.name))
    return ordered[: max(0, int(config.UP_CORE_THREAD_MAX))]


def _target_repeat_indexes(first_index: int, project_count: int, occurrence_count: int) -> list[int]:
    if project_count <= 2 or occurrence_count <= 1:
        return []
    targets: list[int] = []
    # Expanding gaps in project units. This approximates spaced repetition while
    # staying deterministic and independent of calendar dates.
    gap = max(2, int(config.UP_SPIRAL_MIN_GAP))
    cursor = first_index
    for _touch in range(2, occurrence_count + 1):
        cursor += gap
        if cursor >= project_count:
            cursor = project_count - 1
        if cursor > first_index and cursor not in targets:
            targets.append(cursor)
        gap += max(1, int(config.UP_SPIRAL_GAP_GROWTH))
    return targets


def _bucket_for_repeat(touch_index: int, total_occurrences: int) -> str:
    if touch_index <= 1:
        return "can"
    if touch_index >= total_occurrences:
        return "skills"
    return "can"


def _add_spiral_occurrences(blocks: list[CurriculumBlock], nodes: list[PlanNode], dag_payload: dict[str, object]) -> set[str]:
    projects = _flatten_projects(blocks)
    if len(projects) < 3:
        return set()
    direct_edges = _direct_edge_pairs(dag_payload, hard_only=True)
    primary_index = _primary_project_index(projects)
    repeated_threads: set[str] = set()
    max_skills = max(1, int(config.UP_MAX_SKILLS_PER_PROJECT))

    for node in _select_core_threads(nodes, dag_payload):
        first_index = primary_index.get(node.tmp_id)
        if first_index is None:
            continue
        desired = min(max(1, int(config.UP_MAX_THREAD_OCCURRENCES)), max(1, len(projects) // 3 + 1))
        desired = max(int(config.UP_MIN_THREAD_OCCURRENCES), desired)
        desired = min(desired, len(projects))
        targets = _target_repeat_indexes(first_index, len(projects), desired)
        total_occurrences = 1 + len(targets)
        for touch_offset, target_index in enumerate(targets, start=2):
            project = projects[target_index]
            existing_nodes = project.unique_nodes
            if node.tmp_id in {item.tmp_id for item in existing_nodes}:
                continue
            if len(existing_nodes) >= max_skills:
                continue
            if _has_direct_edge(node, existing_nodes, direct_edges):
                continue
            role = "assessment" if touch_offset == total_occurrences else "reinforcement"
            project.occurrences.append(
                SkillOccurrence(
                    node=node,
                    role=role,
                    touch_index=touch_offset,
                    bloom_bucket=_bucket_for_repeat(touch_offset, total_occurrences),
                )
            )
            repeated_threads.add(node.tmp_id)
    return repeated_threads


def build_curriculum_blocks(nodes: list[PlanNode], dag_payload: dict[str, object]) -> tuple[list[CurriculumBlock], dict[str, object]]:
    """Build project blocks and return planner metadata."""
    blocks = _regroup_projects_by_dag_order(_pack_primary_projects(nodes, dag_payload), dag_payload)
    core_threads = _select_core_threads(nodes, dag_payload)
    repeated_threads = _add_spiral_occurrences(blocks, nodes, dag_payload)
    meta = {
        "core_thread_ids": [node.tmp_id for node in core_threads],
        "core_thread_names": [node.name for node in core_threads],
        "repeated_thread_ids": sorted(repeated_threads),
        "repeated_thread_count": len(repeated_threads),
    }
    return blocks, meta
