"""Стадия 2->3: навыки-кандидаты -> prereq-DAG.

Рёбра (структурные + ИИ) -> проверка направления по Блуму + триаж ->
разрыв цикла по мин. уверенности -> транзитивная редукция (networkx).
"""
from __future__ import annotations
import json
import networkx as nx
from . import config, llm
from . import language
from .models import PrereqEdge, SkillCandidate


def looks_corrupted(text: str | None) -> bool:
    if not text:
        return False
    marker_count = text.count("?") + text.count("\ufffd")
    if "??" in text or "\ufffd" in text:
        return True
    return marker_count >= 2 and (marker_count / max(len(text), 1)) >= 0.2


def display_name(cand: SkillCandidate) -> str:
    if cand.canonical_name and cand.resolution in {"matched", "alias", "fuzzy"}:
        return language.localize_skill_label(cand.canonical_name)
    if cand.canonical_name and looks_corrupted(cand.name):
        return language.localize_skill_label(cand.canonical_name)
    return language.localize_skill_label(cand.name)


def display_group(cand: SkillCandidate) -> str:
    if cand.canonical_group and cand.resolution in {"matched", "alias", "fuzzy"}:
        return language.localize_group_label(cand.canonical_group) or language.localize_area_label(cand.canonical_group)
    if cand.canonical_group and looks_corrupted(cand.group):
        return language.localize_group_label(cand.canonical_group) or language.localize_area_label(cand.canonical_group)
    if looks_corrupted(cand.group):
        return "Группа требует проверки"
    return language.localize_group_label(cand.group) or language.localize_area_label(cand.group) or cand.group


def _graph_candidates(cands: list[SkillCandidate]) -> list[SkillCandidate]:
    return [
        cand
        for cand in cands
        if cand.entity_type == "skill" and cand.atomicity == "atomic" and cand.decision == "accepted"
    ]


def propose_edges(cands: list[SkillCandidate]) -> list[PrereqEdge]:
    """Структурные рёбра (учебные карты) + предложения ИИ. tmp_id как узлы."""
    by_name = {c.name: c.tmp_id for c in cands}

    def tid(name_part: str) -> str | None:
        for nm, t in by_name.items():
            if name_part.lower() in nm.lower():
                return t
        return None

    edges: list[PrereqEdge] = []
    # Структурные правила выносятся в конфиг, чтобы DAG-слой не был привязан к одному домену.
    for a, b in config.STRUCTURAL_PREREQ_RULES:
        sa, sb = tid(a), tid(b)
        if sa and sb and sa != sb:
            edges.append(PrereqEdge(src=sa, dst=sb, relation_type="hard", confidence=0.9, source="syllabus"))
    if config.USE_LIVE:
        cl = [{"id": c.tmp_id, "name": c.name, "bloom": c.bloom} for c in cands]
        sys = (
            "Предложи только мягкие методические зависимости между навыками для построения учебной последовательности. "
            "Это не строгие hard prerequisites: hard-связи появляются только из явно заданных структурных правил. "
            "JSON {edges:[{src,dst,confidence,rationale}]}. src/dst только из id. "
            "rationale пиши на русском языке."
        )
        try:
            data = json.loads(llm.content(llm.chat(config.MODEL_PLAN,
                [{"role": "system", "content": sys}, {"role": "user", "content": json.dumps(cl, ensure_ascii=False)}],
                json_mode=True)))
            ids = {c.tmp_id for c in cands}
            for e in data.get("edges", []):
                if e["src"] in ids and e["dst"] in ids:
                    edges.append(PrereqEdge(src=e["src"], dst=e["dst"], relation_type="soft",
                                            confidence=float(e.get("confidence", 0.5)), source="ai",
                                            rationale=e.get("rationale", "")))
        except Exception:
            pass
    else:
        # MOCK: одно ошибочное ребро (создаст цикл) + одно избыточное
        sql, rel, rest, q = tid("SQL"), tid("реляцион"), tid("REST"), tid("очеред")
        if sql and rel:
            edges.append(PrereqEdge(src=sql, dst=rel, relation_type="soft", confidence=0.55, source="ai",
                                    rationale="(ошибочно) SQL раньше БД"))   # цикл рел->SQL->рел
        if rel and rest:
            edges.append(PrereqEdge(src=rel, dst=rest, relation_type="soft", confidence=0.6, source="ai",
                                    rationale="(избыточно) есть путь рел->SQL->REST"))
    return edges


def deduplicate_edges(edges: list[PrereqEdge]) -> list[PrereqEdge]:
    best: dict[tuple[str, str], PrereqEdge] = {}
    for edge in edges:
        key = (edge.src, edge.dst)
        if key not in best or edge.confidence > best[key].confidence:
            best[key] = edge
    return list(best.values())


def triage_edges(edges: list[PrereqEdge], cands: list[SkillCandidate]) -> None:
    bloom = {c.tmp_id: c.bloom for c in cands}
    for e in edges:
        if bloom.get(e.src, 1) > bloom.get(e.dst, 1):
            e.bloom_violation = True
        r = []
        if e.bloom_violation:
            r.append("bloom_direction")
        if e.source == "ai":
            r.append("ai_proposed")
        if e.confidence < config.TAU_EDGE_ACCEPT:
            r.append("low_confidence")
        e.reasons = r
        e.decision = "accept" if not r else "needs_review"


def apply_edge_decision_overrides(edges: list[PrereqEdge], decisions: dict[str, str] | None) -> None:
    """Apply persisted methodologist decisions to proposed edges."""
    if not decisions:
        return
    for edge in edges:
        edge_key = f"{edge.src}->{edge.dst}"
        decision = decisions.get(edge_key)
        if decision == "accepted":
            edge.decision = "accept"
            edge.reasons = ["human_accepted"]
            edge.bloom_violation = False
        elif decision == "rejected":
            edge.decision = "rejected"
            edge.reasons = ["human_rejected"]


def operational_edges(edges: list[PrereqEdge]) -> list[PrereqEdge]:
    """Return only confirmed edges that are allowed to influence the operational DAG."""
    return [edge for edge in edges if edge.decision == "accept"]


def visual_preview_edges(edges: list[PrereqEdge]) -> list[PrereqEdge]:
    """Return proposed edges that are safe enough for display-only DAG preview.

    These edges must not influence persistence or curriculum planning. They are
    useful for methodologists because AI-proposed edges intentionally go to
    review before promotion into the operational DAG.
    """
    preview: list[PrereqEdge] = []
    for edge in edges:
        if edge.decision == "accept":
            preview.append(edge)
            continue
        if edge.decision == "rejected":
            continue
        if edge.source != "ai":
            continue
        if edge.bloom_violation:
            continue
        if edge.confidence < config.TAU_EDGE_ACCEPT:
            continue
        preview.append(edge)
    return preview


def build_dag(edges: list[PrereqEdge], cands: list[SkillCandidate]):
    """Возвращает (DAG, removed_cycle, removed_transitive)."""
    G = nx.DiGraph()
    G.add_nodes_from(c.tmp_id for c in cands)
    for e in edges:
        G.add_edge(e.src, e.dst, conf=e.confidence, edge=e)

    removed_cycle = []
    while True:
        try:
            cyc = nx.find_cycle(G, orientation="original")
        except nx.NetworkXNoCycle:
            break
        ce = [(u, v) for u, v, *_ in cyc]
        u, v = min(ce, key=lambda x: G[x[0]][x[1]]["conf"])
        removed_cycle.append((u, v))
        G.remove_edge(u, v)

    TR = nx.transitive_reduction(G)
    removed_transitive = [(u, v) for u, v in set(G.edges()) - set(TR.edges())]
    DAG = nx.DiGraph()
    DAG.add_nodes_from(G.nodes())
    for u, v in TR.edges():
        DAG.add_edge(u, v, **G[u][v])
    return DAG, removed_cycle, removed_transitive


def build_topological_waves(DAG: nx.DiGraph, cands: list[SkillCandidate]) -> tuple[list[list[str]], list[str]]:
    by_tid = {cand.tmp_id: cand for cand in cands}
    waves: list[list[str]] = []
    order: list[str] = []
    for generation in nx.topological_generations(DAG):
        wave = sorted(generation, key=lambda tid: (by_tid[tid].bloom, display_name(by_tid[tid])))
        waves.append(wave)
        order.extend(wave)
    return waves, order


def build_edge_review_queue(
    edges: list[PrereqEdge],
    removed_cycle: list[tuple[str, str]],
    removed_transitive: list[tuple[str, str]],
    cands: list[SkillCandidate],
) -> list[dict[str, object]]:
    by_tid = {cand.tmp_id: cand for cand in cands}
    display_names = {cand.tmp_id: display_name(cand) for cand in cands}
    removed_cycle_set = set(removed_cycle)
    removed_transitive_set = set(removed_transitive)
    review_queue: list[dict[str, object]] = []

    for edge in edges:
        key = (edge.src, edge.dst)
        if edge.decision == "needs_review" and key not in removed_cycle_set and key not in removed_transitive_set:
            review_queue.append(
                {
                    "edge_key": f"{edge.src}->{edge.dst}",
                    "src_id": edge.src,
                    "dst_id": edge.dst,
                    "edge_label": f"{display_names[edge.src]} -> {display_names[edge.dst]}",
                    "reason_code": ",".join(edge.reasons) or "needs_review",
                    "severity": "warning" if edge.bloom_violation else "info",
                    "status": "open",
                    "confidence": edge.confidence,
                    "source": edge.source,
                    "relation_type": edge.relation_type,
                    "reasons": edge.reasons,
                }
            )

    for src, dst in removed_cycle:
        review_queue.append(
            {
                "edge_key": f"{src}->{dst}",
                "src_id": src,
                "dst_id": dst,
                "edge_label": f"{display_names[src]} -> {display_names[dst]}",
                "reason_code": "cycle_broken",
                "severity": "warning",
                "status": "open",
                "confidence": None,
                "source": "pipeline",
                "relation_type": "soft",
                "reasons": ["cycle_broken"],
            }
        )

    for src, dst in removed_transitive:
        review_queue.append(
            {
                "edge_key": f"{src}->{dst}",
                "src_id": src,
                "dst_id": dst,
                "edge_label": f"{display_names[src]} -> {display_names[dst]}",
                "reason_code": "redundant_transitive",
                "severity": "info",
                "status": "open",
                "confidence": None,
                "source": "pipeline",
                "relation_type": "soft",
                "reasons": ["redundant_transitive"],
            }
        )

    return review_queue


def build_dag_payload(
    edges: list[PrereqEdge],
    DAG: nx.DiGraph,
    removed_cycle: list[tuple[str, str]],
    removed_transitive: list[tuple[str, str]],
    cands: list[SkillCandidate],
) -> dict[str, object]:
    by_tid = {cand.tmp_id: cand for cand in cands}
    display_names = {cand.tmp_id: display_name(cand) for cand in cands}
    display_groups = {cand.tmp_id: display_group(cand) for cand in cands}
    waves, order = build_topological_waves(DAG, cands)
    review_queue = build_edge_review_queue(edges, removed_cycle, removed_transitive, cands)
    final_edges = []
    for src, dst in DAG.edges():
        edge = DAG[src][dst].get("edge")
        final_edges.append(
            {
                "src_id": src,
                "dst_id": dst,
                "src": display_names[src],
                "dst": display_names[dst],
                "source": edge.source if edge else "pipeline",
                "relation_type": edge.relation_type if edge else "soft",
                "relation_label": (
                    "Структурный пререквизит"
                    if edge and edge.relation_type == "hard"
                    else "Мягкая методическая связь"
                ),
                "confidence": edge.confidence if edge else DAG[src][dst].get("conf"),
                "decision": edge.decision if edge else "accept",
                "reasons": edge.reasons if edge else [],
            }
        )

    return {
        "nodes": DAG.number_of_nodes(),
        "edges": DAG.number_of_edges(),
        "removed_cycle": len(removed_cycle),
        "removed_transitive": len(removed_transitive),
        "acyclic": nx.is_directed_acyclic_graph(DAG),
        "waves": [
            [
                {"id": tid, "name": display_names[tid], "group": display_groups[tid], "bloom": by_tid[tid].bloom}
                for tid in wave
            ]
            for wave in waves
        ],
        "order": [
            {"id": tid, "name": display_names[tid], "group": display_groups[tid], "bloom": by_tid[tid].bloom}
            for tid in order
        ],
        "final_edges": final_edges,
        "edge_review_queue": review_queue,
    }


def add_visual_preview_payload(dag_payload: dict[str, object], edges: list[PrereqEdge], cands: list[SkillCandidate]) -> None:
    """Attach display-only DAG preview without changing the operational DAG."""
    preview_edges = visual_preview_edges(edges)
    if not preview_edges:
        dag_payload["visual_edges"] = dag_payload.get("final_edges", [])
        dag_payload["visual_waves"] = dag_payload.get("waves", [])
        dag_payload["visual_order"] = dag_payload.get("order", [])
        dag_payload["visual_edge_count"] = int(dag_payload.get("edges") or 0)
        dag_payload["preview_edge_count"] = 0
        return

    preview_dag, preview_removed_cycle, preview_removed_transitive = build_dag(preview_edges, cands)
    preview_payload = build_dag_payload(preview_edges, preview_dag, preview_removed_cycle, preview_removed_transitive, cands)
    dag_payload["visual_edges"] = preview_payload["final_edges"]
    dag_payload["visual_waves"] = preview_payload["waves"]
    dag_payload["visual_order"] = preview_payload["order"]
    dag_payload["visual_edge_count"] = preview_payload["edges"]
    dag_payload["preview_edge_count"] = max(0, len(preview_edges) - int(dag_payload.get("accepted_edge_count") or 0))
    dag_payload["visual_removed_cycle"] = preview_payload["removed_cycle"]
    dag_payload["visual_removed_transitive"] = preview_payload["removed_transitive"]


def run(cands: list[SkillCandidate], edge_decisions: dict[str, str] | None = None):
    used_candidates = _graph_candidates(cands)
    all_edges = deduplicate_edges(propose_edges(used_candidates))
    triage_edges(all_edges, used_candidates)
    apply_edge_decision_overrides(all_edges, edge_decisions)
    accepted_edges = operational_edges(all_edges)
    DAG, removed_cycle, removed_transitive = build_dag(accepted_edges, used_candidates)
    dag_payload = build_dag_payload(all_edges, DAG, removed_cycle, removed_transitive, used_candidates)
    dag_payload["used_candidate_ids"] = [cand.tmp_id for cand in used_candidates]
    dag_payload["candidate_edge_count"] = len(all_edges)
    dag_payload["accepted_edge_count"] = len(accepted_edges)
    add_visual_preview_payload(dag_payload, all_edges, used_candidates)
    return all_edges, DAG, removed_cycle, removed_transitive, dag_payload
