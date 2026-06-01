"""Стадия 2->3: навыки-кандидаты -> prereq-DAG.

Рёбра (структурные + ИИ) -> проверка направления по Блуму + триаж ->
разрыв цикла по мин. уверенности -> транзитивная редукция (networkx).
"""
from __future__ import annotations
import json
import networkx as nx
from . import config, llm
from .models import PrereqEdge, SkillCandidate, BLOOM


def _bloom_of(cand: SkillCandidate) -> int:
    return cand.bloom


def propose_edges(cands: list[SkillCandidate]) -> list[PrereqEdge]:
    """Структурные рёбра (учебные карты) + предложения ИИ. tmp_id как узлы."""
    by_name = {c.name: c.tmp_id for c in cands}

    def tid(name_part: str) -> str | None:
        for nm, t in by_name.items():
            if name_part.lower() in nm.lower():
                return t
        return None

    edges: list[PrereqEdge] = []
    # структурные (mined)
    structural = [("реляцион", "SQL"), ("SQL", "REST"), ("REST", "очеред")]
    for a, b in structural:
        sa, sb = tid(a), tid(b)
        if sa and sb and sa != sb:
            edges.append(PrereqEdge(src=sa, dst=sb, relation_type="hard", confidence=0.9, source="syllabus"))
    if config.USE_LIVE:
        cl = [{"id": c.tmp_id, "name": c.name, "bloom": c.bloom} for c in cands]
        sys = ("Предложи рёбра пререквизитов. JSON {edges:[{src,dst,confidence,rationale}]}. "
               "src/dst только из id.")
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


def build_dag(edges: list[PrereqEdge], cands: list[SkillCandidate]):
    """Возвращает (DAG, removed_cycle, removed_transitive)."""
    G = nx.DiGraph()
    G.add_nodes_from(c.tmp_id for c in cands)
    for e in edges:
        G.add_edge(e.src, e.dst, conf=e.confidence)

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


def run(cands: list[SkillCandidate]):
    edges = propose_edges(cands)
    triage_edges(edges, cands)
    DAG, removed_cycle, removed_transitive = build_dag(edges, cands)
    return edges, DAG, removed_cycle, removed_transitive
