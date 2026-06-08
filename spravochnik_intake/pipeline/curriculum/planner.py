"""Deterministic spiral curriculum planner.

The planner intentionally does not call LLMs. It transforms accepted skills and
the prerequisite DAG into project blueprints that are denser and more
pedagogically useful than a one-skill-per-project topological walk.
"""

from __future__ import annotations

from collections import Counter
import re
import networkx as nx

from .. import config
from .domain import CurriculumBlock, PlanNode, ProjectBlueprint, SkillOccurrence

_DANGLING_TAIL_WORDS = {
    "и",
    "или",
    "в",
    "во",
    "на",
    "для",
    "по",
    "с",
    "со",
    "к",
    "ко",
    "о",
    "об",
    "от",
    "до",
    "из",
}


def _strip_dangling_tail(text: str) -> str:
    """Remove half-open clauses that appear after compacting generated labels."""
    cleaned = re.sub(r"\([^)]*$", "", text).strip(" .,-:;(")
    words = cleaned.split()
    while words and words[-1].casefold().strip(" .,-:;()") in _DANGLING_TAIL_WORDS:
        words.pop()
    return " ".join(words).strip(" .,-:;")


def _drop_latin_parenthetical_notes(text: str) -> str:
    """Remove English glossary notes from Russian curriculum labels."""
    return re.sub(r"\s*\([^)]*[A-Za-z][^)]*\)", "", text).strip()


def _limit_on_word_boundary(text: str, *, max_chars: int) -> str:
    """Shorten text without cutting words or leaving unfinished parentheses."""
    if len(text) <= max_chars:
        return _strip_dangling_tail(text) or text.strip(" .,-")
    limit = max(12, max_chars - 1)
    candidate = text[:limit].rstrip()
    boundary = candidate.rfind(" ")
    if boundary >= max(12, limit // 2):
        candidate = candidate[:boundary]
    candidate = _strip_dangling_tail(candidate)
    return f"{candidate}…" if candidate else "…"


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


def _compact_text(value: str, *, max_words: int = 6, max_chars: int = 72) -> str:
    """Keep generated curriculum labels compact and domain-neutral."""
    text = " ".join(str(value or "").replace("—", "-").split()).strip(" .,-")
    if not text:
        return "Общее"
    text = text.split(":", 1)[0].strip()
    text = _drop_latin_parenthetical_notes(text)
    words = text.split()
    shortened_by_words = False
    if len(words) > max_words:
        text = " ".join(words[:max_words])
        shortened_by_words = True
    text = _strip_dangling_tail(text)
    if len(text) > max_chars:
        text = _limit_on_word_boundary(text, max_chars=max_chars)
    elif shortened_by_words and text:
        text = f"{text}…"
    return text or "Общее"


def _project_theme_for(node: PlanNode) -> str:
    """Use catalog-derived semantics only: coverage area, skill group, then generic fallback."""
    return _compact_text(node.block_key or node.group or "Общее", max_words=6, max_chars=72)


_ARTIFACT_FAMILY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("analysis", ("анализ", "оцен", "исслед", "выяв", "диагност", "измер", "интерпрет", "сравн", "аудит")),
    ("document", ("документ", "опис", "оформ", "подготов", "состав", "регламент", "чек-лист", "шаблон", "гайд", "отчет", "отчёт")),
    ("configuration", ("настрой", "развер", "внедр", "интегр", "автоматиз", "конфиг", "подключ", "администр")),
    ("design", ("проектир", "модел", "планир", "определ", "формулир", "выбор", "специфиц", "приорит")),
    ("production", ("созда", "собир", "разработ", "реализ", "постро", "изготов", "код", "программ")),
)


_ARTIFACT_FAMILY_LABELS = {
    "analysis": "аналитический вывод",
    "document": "комплект документов",
    "configuration": "рабочая настройка",
    "design": "проектное решение",
    "production": "созданный продуктовый результат",
    "practice": "практический результат",
}


def _norm_text(value: object) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я+ ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _artifact_family_for(node: PlanNode) -> str:
    """Classify a checked artifact type without using domain-specific templates."""
    text = " ".join(
        [
            node.name,
            node.group,
            node.block_key,
            " ".join(node.outcomes_know),
            " ".join(node.outcomes_can),
            " ".join(node.outcomes_skills),
            " ".join(node.tools),
        ]
    ).casefold().replace("ё", "е")
    for family, hints in _ARTIFACT_FAMILY_PATTERNS:
        if any(hint in text for hint in hints):
            return family
    if node.bloom >= 5:
        return "production"
    if node.bloom >= 4:
        return "design"
    return "practice"


def _artifact_key_for(node: PlanNode) -> str:
    return f"{_project_theme_for(node)}::{_artifact_family_for(node)}"


def _node_scope_text(node: PlanNode) -> str:
    return _norm_text(" ".join([node.name, node.group, node.block_key]))


def _template_scopes(template: dict[str, object]) -> list[dict[str, object]]:
    raw = template.get("scopes")
    return [scope for scope in raw if isinstance(scope, dict)] if isinstance(raw, list) else []


def _template_scope_score(node: PlanNode, template: dict[str, object]) -> float:
    family = str(template.get("artifact_family") or "").strip()
    if family and family != _artifact_family_for(node):
        return 0.0

    scopes = _template_scopes(template)
    if not scopes:
        return 0.1

    node_text = _node_scope_text(node)
    best = 0.0
    for scope in scopes:
        scope_type = str(scope.get("scope_type") or "").strip()
        if scope_type == "any":
            best = max(best, 0.6 * float(scope.get("weight", 1.0) or 1.0))
            continue
        scope_name = str(scope.get("normalized_scope_name") or scope.get("scope_name") or "").strip()
        normalized_scope = _norm_text(scope_name)
        if not normalized_scope:
            continue
        scope_tokens = set(normalized_scope.split())
        node_tokens = set(node_text.split())
        overlap = len(scope_tokens & node_tokens) / max(len(scope_tokens), 1)
        if normalized_scope in node_text:
            overlap = max(overlap, 1.0)
        best = max(best, overlap * float(scope.get("weight", 1.0) or 1.0))
    return best


def _best_template_for_node(node: PlanNode, artifact_templates: list[dict[str, object]]) -> dict[str, object] | None:
    scored = [
        (template, _template_scope_score(node, template))
        for template in artifact_templates
        if isinstance(template, dict)
    ]
    scored = [(template, score) for template, score in scored if score >= 0.5]
    if not scored:
        return None
    return max(scored, key=lambda item: (item[1], -int(item[0].get("priority", 100) or 100)))[0]


def _render_pattern(pattern: object, *, nodes: list[PlanNode], block_key: str, artifact_family: str, artifact: str = "") -> str:
    text = str(pattern or "").strip()
    if not text:
        return ""
    skills = ", ".join(node.name for node in nodes)
    first_skill = nodes[0].name if nodes else ""
    try:
        return text.format(
            theme=block_key,
            skills=skills,
            first_skill=first_skill,
            artifact=artifact,
            artifact_family=_ARTIFACT_FAMILY_LABELS.get(artifact_family, artifact_family),
        )
    except (KeyError, IndexError, ValueError):
        return text


def _template_artifact_for(nodes: list[PlanNode], block_key: str, artifact_family: str, template: dict[str, object] | None) -> str:
    if not template:
        return ""
    rendered = _render_pattern(
        template.get("artifact_description"),
        nodes=nodes,
        block_key=block_key,
        artifact_family=artifact_family,
    )
    return rendered


def _template_title_for(nodes: list[PlanNode], block_key: str, artifact_family: str, template: dict[str, object] | None) -> str:
    if not template:
        return ""
    pattern = str(template.get("project_name_pattern") or "").strip()
    rendered = _render_pattern(
        pattern,
        nodes=nodes,
        block_key=block_key,
        artifact_family=artifact_family,
    )
    # Project-name patterns can expand into long skill lists. If that happens,
    # use the accepted template title as the stable human-readable project name.
    if rendered and ("{" not in pattern or len(rendered) <= 72):
        return rendered
    title = _render_pattern(
        template.get("title"),
        nodes=nodes,
        block_key=block_key,
        artifact_family=artifact_family,
    )
    return title or rendered


def _template_enrichment_for(
    nodes: list[PlanNode],
    block_key: str,
    artifact_family: str,
    artifact: str,
    template: dict[str, object] | None,
) -> dict[str, str]:
    if not template:
        return {}
    return {
        "materials": _render_pattern(template.get("materials_pattern"), nodes=nodes, block_key=block_key, artifact_family=artifact_family, artifact=artifact),
        "storytelling": _render_pattern(template.get("storytelling_pattern"), nodes=nodes, block_key=block_key, artifact_family=artifact_family, artifact=artifact),
        "validation_criteria": _render_pattern(template.get("validation_criteria"), nodes=nodes, block_key=block_key, artifact_family=artifact_family, artifact=artifact),
    }


def _artifact_for(nodes: list[PlanNode], block_key: str, artifact_family: str) -> str:
    theme = _compact_text(block_key, max_words=6, max_chars=72)
    family_label = _ARTIFACT_FAMILY_LABELS.get(artifact_family, _ARTIFACT_FAMILY_LABELS["practice"])
    if len(nodes) == 1:
        return f"Проверяемый артефакт ({family_label}) по навыку «{_compact_text(nodes[0].name, max_words=8, max_chars=90)}»"
    labels = [_compact_text(node.name, max_words=5, max_chars=56) for node in nodes[:3]]
    suffix = f" и ещё {len(nodes) - 3}" if len(nodes) > 3 else ""
    return f"Интегративный артефакт ({family_label}) по теме «{theme}»: {', '.join(labels)}{suffix}"


def _project_title_for(block_key: str, chunk_index: int, chunk_count: int) -> str:
    suffix = f" {chunk_index}" if chunk_count > 1 else ""
    return f"{_compact_text(block_key, max_words=4, max_chars=44)}{suffix}"


def _ordered_nodes(nodes: list[PlanNode], dag_payload: dict[str, object]) -> list[PlanNode]:
    position = _dag_position(dag_payload)
    return sorted(nodes, key=lambda item: (position.get(item.tmp_id, 10**9), item.bloom, item.name))


def _project_from_nodes(
    nodes: list[PlanNode],
    *,
    block_key: str,
    artifact: str,
    artifact_key: str,
    artifact_family: str,
    artifact_template_code: str,
    enrichment: dict[str, str],
    title: str,
    project_kind: str,
) -> ProjectBlueprint:
    return ProjectBlueprint(
        occurrences=[SkillOccurrence(item, role="primary", touch_index=1) for item in nodes],
        block_key=block_key,
        artifact=artifact,
        artifact_key=artifact_key,
        artifact_family=artifact_family,
        artifact_template_code=artifact_template_code,
        enrichment=enrichment,
        title=title,
        project_kind=project_kind,
    )


def _split_nodes_for_project(
    nodes: list[PlanNode],
    dag_payload: dict[str, object],
    *,
    max_skills: int,
) -> list[list[PlanNode]]:
    """Split nodes into project chunks without placing hard prerequisites together."""
    direct_edges = _direct_edge_pairs(dag_payload, hard_only=True)
    chunks: list[list[PlanNode]] = []
    current: list[PlanNode] = []
    for node in nodes:
        can_append = current and len(current) < max_skills and not _has_direct_edge(node, current, direct_edges)
        if can_append:
            current.append(node)
            continue
        if current:
            chunks.append(current)
        current = [node]
    if current:
        chunks.append(current)
    return chunks


def _pack_dynamic_artifact_projects(
    nodes: list[PlanNode],
    dag_payload: dict[str, object],
    artifact_templates: list[dict[str, object]] | None = None,
) -> tuple[list[ProjectBlueprint], dict[str, object]]:
    max_skills = max(1, int(config.UP_MAX_SKILLS_PER_PROJECT))
    grouped: dict[str, list[PlanNode]] = {}
    group_order: list[str] = []
    group_meta: dict[str, tuple[str, str, dict[str, object] | None]] = {}
    templates = artifact_templates or []
    for node in _ordered_nodes(nodes, dag_payload):
        template = _best_template_for_node(node, templates)
        dynamic_key = _artifact_key_for(node)
        template_code = str((template or {}).get("code") or "").strip()
        artifact_key = f"{dynamic_key}::template:{template_code}" if template_code else dynamic_key
        if artifact_key not in grouped:
            grouped[artifact_key] = []
            group_order.append(artifact_key)
            group_meta[artifact_key] = (_project_theme_for(node), _artifact_family_for(node), template)
        grouped[artifact_key].append(node)

    projects: list[ProjectBlueprint] = []
    assignment: dict[str, str] = {}
    split_count = 0

    for artifact_key in group_order:
        block_key, artifact_family, template = group_meta[artifact_key]
        chunks = _split_nodes_for_project(grouped[artifact_key], dag_payload, max_skills=max_skills)
        split_count += max(0, len(chunks) - 1)
        for chunk_index, chunk in enumerate(chunks, start=1):
            template_artifact = _template_artifact_for(chunk, block_key, artifact_family, template)
            artifact = template_artifact or _artifact_for(chunk, block_key, artifact_family)
            template_title = _template_title_for(chunk, block_key, artifact_family, template)
            projects.append(
                _project_from_nodes(
                    chunk,
                    block_key=block_key,
                    artifact=artifact,
                    artifact_key=artifact_key,
                    artifact_family=artifact_family,
                    artifact_template_code=str((template or {}).get("code") or "").strip(),
                    enrichment=_template_enrichment_for(chunk, block_key, artifact_family, artifact, template),
                    title=template_title or _project_title_for(block_key, chunk_index, len(chunks)),
                    project_kind="dynamic_artifact",
                )
            )
            for node in chunk:
                assignment[node.tmp_id] = artifact_key

    meta = {
        "artifact_first": True,
        "artifact_template_count": 0,
        "artifact_project_count": len(projects),
        "artifact_split_count": split_count,
        "dynamic_group_count": len(group_order),
        "artifact_family_counts": dict(Counter(family for _theme, family, _template in group_meta.values())),
        "db_template_count": len(templates),
        "db_template_project_count": len([project for project in projects if project.artifact_template_code]),
        "unassigned_node_count": 0,
        "assignment": assignment,
    }
    return projects, meta


def _reorder_projects_by_hard_edges(projects: list[ProjectBlueprint], dag_payload: dict[str, object]) -> list[ProjectBlueprint]:
    if len(projects) <= 1:
        return projects
    project_by_node: dict[str, int] = {}
    for project_index, project in enumerate(projects):
        for node in project.unique_nodes:
            project_by_node.setdefault(node.tmp_id, project_index)

    graph = nx.DiGraph()
    graph.add_nodes_from(range(len(projects)))
    for src_id, dst_id in _direct_edge_pairs(dag_payload, hard_only=True):
        src_project = project_by_node.get(src_id)
        dst_project = project_by_node.get(dst_id)
        if src_project is None or dst_project is None or src_project == dst_project:
            continue
        graph.add_edge(src_project, dst_project)

    if not nx.is_directed_acyclic_graph(graph):
        return projects
    ordered_indexes = list(nx.lexicographical_topological_sort(graph, key=lambda index: index))
    return [projects[index] for index in ordered_indexes]


def _blocks_from_projects(projects: list[ProjectBlueprint]) -> list[CurriculumBlock]:
    blocks: list[CurriculumBlock] = []
    chunk_size = max(1, int(config.UP_MAX_PROJECTS_PER_BLOCK))
    for offset in range(0, len(projects), chunk_size):
        chunk = projects[offset : offset + chunk_size]
        blocks.append(CurriculumBlock(block_keys=_ordered_block_keys(chunk), projects=chunk))
    return blocks


def _pack_dynamic_artifact_blocks(
    nodes: list[PlanNode],
    dag_payload: dict[str, object],
    artifact_templates: list[dict[str, object]] | None = None,
) -> tuple[list[CurriculumBlock], dict[str, object]]:
    projects, meta = _pack_dynamic_artifact_projects(nodes, dag_payload, artifact_templates)
    projects = _reorder_projects_by_hard_edges(projects, dag_payload)
    return _blocks_from_projects(projects), meta


def _flatten_projects(blocks: list[CurriculumBlock]) -> list[ProjectBlueprint]:
    return [project for block in blocks for project in block.projects]


def _ordered_block_keys(projects: list[ProjectBlueprint]) -> tuple[str, ...]:
    keys: list[str] = []
    for project in projects:
        if project.block_key and project.block_key not in keys:
            keys.append(project.block_key)
    return tuple(keys) or ("Общее",)


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


def build_curriculum_blocks(
    nodes: list[PlanNode],
    dag_payload: dict[str, object],
    artifact_templates: list[dict[str, object]] | None = None,
) -> tuple[list[CurriculumBlock], dict[str, object]]:
    """Build project blocks and return planner metadata."""
    blocks, artifact_meta = _pack_dynamic_artifact_blocks(nodes, dag_payload, artifact_templates)
    core_threads = _select_core_threads(nodes, dag_payload)
    repeated_threads = _add_spiral_occurrences(blocks, nodes, dag_payload)
    meta = {
        **artifact_meta,
        "artifact_match_count": 0,
        "core_thread_ids": [node.tmp_id for node in core_threads],
        "core_thread_names": [node.name for node in core_threads],
        "repeated_thread_ids": sorted(repeated_threads),
        "repeated_thread_count": len(repeated_threads),
    }
    return blocks, meta
