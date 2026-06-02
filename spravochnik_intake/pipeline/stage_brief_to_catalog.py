"""Стадия 1->2: бриф -> навыки-кандидаты справочника.

decompose -> grounded-поиск -> evidence -> синтез -> резолв (репозиторий)
-> жюри по серой зоне -> триаж.
"""
from __future__ import annotations
import json
from datetime import date
from . import config, llm
from . import stage_atomize
from .models import Evidence, IndicatorSpec, SkillCandidate
from .catalog_repo import CatalogRepo


def normalize_bloom(value: str | None) -> str:
    mapping = {
        "remember": "remember",
        "recall": "remember",
        "knowledge": "remember",
        "understand": "understand",
        "comprehend": "understand",
        "apply": "apply",
        "application": "apply",
        "analyze": "analyze",
        "analyse": "analyze",
        "evaluate": "evaluate",
        "evaluation": "evaluate",
        "create": "create",
        "creation": "create",
        "знает": "remember",
        "понимает": "understand",
        "умеет": "apply",
        "анализирует": "analyze",
        "оценивает": "evaluate",
        "создает": "create",
        "создаёт": "create",
    }
    key = (value or "understand").strip().casefold()
    return mapping.get(key, "understand")


# --------- decompose ---------
def decompose(brief: str) -> dict:
    if config.USE_LIVE:
        sys = ("Разложи бриф в JSON: role, seniority, domain, sub_queries(list 3-4). Только JSON.")
        return json.loads(llm.content(llm.chat(config.MODEL_PLAN,
            [{"role": "system", "content": sys}, {"role": "user", "content": brief}], json_mode=True)))
    brief_lower = brief.lower()
    if "предпринимател" in brief_lower or "стартап" in brief_lower or "тз на продукт" in brief_lower:
        return {
            "role": "Технологический предприниматель",
            "seniority": "начинающий",
            "domain": "технологический бизнес / цифровые продукты",
            "sub_queries": [
                "формулирование и анализ проблемы",
                "ai-инструменты в маркетинге",
                "основные бизнес-функции стартапа",
                "сегментация клиентов и продуктовые метрики",
            ],
        }
    return {"role": "Backend разработчик на Python", "seniority": "junior", "domain": "финтех",
            "sub_queries": ["junior backend требования", "работа с БД и SQL", "REST API",
                            "очереди сообщений", "контейнеризация Docker"]}


# --------- grounded-поиск -> evidence ---------
def search(query: str) -> list[dict]:
    if config.USE_LIVE:
        sys = ("Найди требуемые навыки. JSON-массив {claim, source_type, url, snippet}. "
               "source_type: vacancy|framework|syllabus.")
        try:
            resp = llm.chat(config.MODEL_SEARCH, [{"role": "system", "content": sys}, {"role": "user", "content": query}])
            items = json.loads(llm.content(resp))
        except Exception:
            items = []
        cits = llm.citations(resp) if items else []
        for it in items:
            it.setdefault("url", cits[0] if cits else "")
            it.setdefault("snippet", "")
            it.setdefault("retrieved_at", date.today().isoformat())
        return items
    today = date.today().isoformat()
    DB = {
        "SQL": [("Уверенный SQL: SELECT, JOIN", "vacancy", "https://hh.ru/v/1", "SQL, JOIN, индексы"),
                ("Основы реляционных БД", "framework", "https://esco.ec.europa.eu/rdb", "relational db")],
        "REST": [("Проектирование REST API", "vacancy", "https://hh.ru/v/2", "REST API, HTTP"),
                 ("Принципы REST", "syllabus", "https://roadmap.sh/backend", "REST design")],
        "очеред": [("Работа с очередями сообщений", "vacancy", "https://hh.ru/v/3", "RabbitMQ/Kafka")],
        "Docker": [("Контейнеризация Docker", "syllabus", "https://roadmap.sh/devops", "Dockerfile, образы")],
        "требован": [("Git в командной работе", "vacancy", "https://hh.ru/v/4", "Git, ветки, review")],
        "проблем": [("Discovery: выявление проблем клиента", "framework", "https://example.org/discovery", "JTBD, problem framing")],
        "ai-инструменты в маркетинге": [("AI-маркетинг: генерация креативов, аналитика", "syllabus", "https://example.org/ai-mkt", "AI marketing")],
        "стартапа": [("Базовые функции стартапа", "framework", "https://example.org/startup-fn", "startup functions")],
        "метрики": [("Продуктовые метрики и сегментация", "syllabus", "https://example.org/product-analytics", "product analytics")],
    }
    out = []
    ql = query.lower()
    for key, items in DB.items():
        if key.lower() in ql:
            for claim, st, url, snip in items:
                out.append({"claim": claim, "source_type": st, "url": url, "snippet": snip, "retrieved_at": today})
    return out


def gather_evidence(sub_queries: list[str]) -> list[Evidence]:
    ev, n = [], 0
    for q in sub_queries:
        for h in search(q):
            n += 1
            ev.append(Evidence(id=f"E{n:02d}", **{k: h[k] for k in ("claim", "source_type", "url", "snippet", "retrieved_at")}))
    # дедуп по (claim,url)
    seen, out = set(), []
    for e in ev:
        k = (e.claim.lower(), e.url)
        if k not in seen:
            seen.add(k); out.append(e)
    return out


# --------- синтез кандидатов (с Блумом и инструментами) ---------
def synthesize(evidence: list[Evidence], spec: dict) -> list[SkillCandidate]:
    ev_ids = [e.id for e in evidence]
    if config.USE_LIVE:
        cl = [{"id": e.id, "claim": e.claim, "type": e.source_type} for e in evidence]
        sys = ("Сгруппируй evidence в навыки-кандидаты. Строгий JSON "
               "{candidates:[{name,group,indicators:[{text,bloom}],tools,evidence_ids}]}. "
               "evidence_ids только из предоставленных. Навык без evidence не включай.")
        data = json.loads(llm.content(llm.chat(config.MODEL_PLAN,
            [{"role": "system", "content": sys}, {"role": "user", "content": json.dumps({"evidence": cl}, ensure_ascii=False)}],
            json_mode=True)))
        items = data.get("candidates", [])
        out = []
        for i, it in enumerate(items, 1):
            ids = [x for x in it.get("evidence_ids", []) if x in ev_ids]
            if not ids:
                continue
            out.append(SkillCandidate(tmp_id=f"C{i:02d}", name=it["name"], group=it.get("group", ""),
                indicators=[
                    IndicatorSpec(text=ind["text"], bloom=normalize_bloom(ind.get("bloom")))
                    for ind in it.get("indicators", [])
                    if ind.get("text")
                ],
                tools=it.get("tools", []), evidence_ids=ids))
        return out
    # MOCK: реалистичные кандидаты под бриф (резолв пойдёт против реального каталога)
    role = spec.get("role", "").lower()
    if "предпринимател" in role or "стартап" in role:
        proto = [
            ("Формулирование и анализ проблемы", "Product Management", [("Формулирует и анализирует проблему", "apply")], []),
            ("Использование AI-инструментов в маркетинге", "Marketing", [("Применяет AI в маркетинге", "apply")], ["GPT"]),
            ("Основные бизнес-функции технологического стартапа", "Startup Management", [("Понимает функции стартапа", "understand")], []),
            ("Анализ и сегментация клиентов, метрики и продуктовый анализ", "Product Management", [("Анализирует клиентов и метрики", "analyze")], []),
        ]
        out = []
        for i, (name, grp, inds, tools) in enumerate(proto, 1):
            out.append(
                SkillCandidate(
                    tmp_id=f"C{i:02d}",
                    name=name,
                    group=grp,
                    indicators=[IndicatorSpec(text=text, bloom=bloom) for text, bloom in inds],
                    tools=tools,
                    evidence_ids=ev_ids[:2] or ev_ids[:1],
                )
            )
        return out

    def by_kw(*kw):
        return [e.id for e in evidence if any(k.lower() in (e.claim + " " + e.snippet).lower() for k in kw)]
    proto = [
        ("Работа с языком SQL", "Данные", [("Знает SELECT/JOIN", "understand"), ("Пишет агрегации", "apply")], ["PostgreSQL"], ("SQL",)),
        ("Понимание основ реляционных баз данных", "Данные", [("Понимает нормализацию", "understand")], ["PostgreSQL"], ("реляц", "relational")),
        ("Работа с REST API", "Сервисы", [("Проектирует контракт", "apply")], ["OpenAPI"], ("REST",)),
        ("Базовая работа с очередями сообщений", "Интеграции", [("Понимает pub/sub", "apply")], ["RabbitMQ"], ("очеред",)),
        ("Работа с Docker", "Инфраструктура", [("Пишет Dockerfile", "apply")], ["Docker"], ("Docker", "контейнер")),
        ("Применение системы контроля версий Git", "Командная работа", [("Работает с ветками", "apply")], ["Git"], ("Git",)),
    ]
    out = []
    for i, (name, grp, inds, tools, kw) in enumerate(proto, 1):
        ids = by_kw(*kw)
        if not ids:
            continue
        out.append(SkillCandidate(tmp_id=f"C{i:02d}", name=name, group=grp,
            indicators=[IndicatorSpec(text=t, bloom=b) for t, b in inds], tools=tools, evidence_ids=ids))
    return out


def _confidence(cand: SkillCandidate, evidence: list[Evidence]) -> float:
    evs = [e for e in evidence if e.id in cand.evidence_ids]
    fw = any(e.source_type in ("framework", "syllabus") for e in evs)
    return round(min(min(0.5 + 0.2 * len(evs), 0.95) + (0.1 if fw else 0.0), 0.97), 2)


# --------- жюри по серой зоне + триаж ---------
def _juror(model: str, cand: SkillCandidate) -> int:
    n = len(set(cand.evidence_ids))
    if model.startswith("openai"):
        return 1
    if model.startswith("anthropic"):
        return 1 if n >= 2 else 0
    return 0 if (cand.resolution == "new" and cand.bloom >= 4) else 1


def _needs_panel(cand: SkillCandidate) -> bool:
    return not (cand.resolution in ("matched", "alias") and cand.confidence >= config.TAU_CONFIDENCE)


def _is_for_resolve(cand: SkillCandidate) -> bool:
    return cand.entity_type == "skill" and cand.atomicity == "atomic"


def atomize_candidates(cands: list[SkillCandidate]) -> list[SkillCandidate]:
    return stage_atomize.run(cands)


def resolve_candidates(cands: list[SkillCandidate], evidence: list[Evidence], repo: CatalogRepo) -> None:
    for cand in cands:
        if not _is_for_resolve(cand):
            continue
        cand.confidence = _confidence(cand, evidence)
        repo.resolve(cand)


def select_council_candidates(cands: list[SkillCandidate]) -> list[SkillCandidate]:
    return [cand for cand in cands if _is_for_resolve(cand) and _needs_panel(cand)]


def run_council(cands: list[SkillCandidate]) -> dict[str, int]:
    council_candidates = select_council_candidates(cands)
    if config.USE_COUNCIL:
        for cand in council_candidates:
            votes = [_juror(model, cand) for model in config.MODEL_PANEL]
            cand.council_ran = True
            cand.council_agreement = round(sum(votes) / len(votes), 2)
            cand.confidence = round(0.6 * cand.confidence + 0.4 * cand.council_agreement, 2)
    return {
        "sent_to_council": len(council_candidates),
        "council_executed": len([cand for cand in cands if cand.council_ran]),
    }


def triage_candidates(cands: list[SkillCandidate]) -> None:
    for c in cands:
        if not _is_for_resolve(c):
            continue
        r = []
        n = len(set(c.evidence_ids))
        if c.resolution == "new":
            r.append("novel_skill")
        if c.resolution == "fuzzy":
            r.append("fuzzy_match_ambiguous")
        if c.confidence < config.TAU_CONFIDENCE:
            r.append("low_confidence")
        if n < config.MIN_SOURCES:
            r.append("single_source")
        if c.council_ran and c.council_agreement is not None and c.council_agreement < config.COUNCIL_AGREE_OK:
            r.append("council_split")
        c.decision = "accepted" if not r else "needs_review"
        c.reasons = r


def build_candidate_metrics(cands: list[SkillCandidate]) -> dict[str, int]:
    resolved_candidates = [cand for cand in cands if _is_for_resolve(cand)]
    return {
        "total_candidates": len(cands),
        "atomic_skill_candidates": len(resolved_candidates),
        "composite_candidates": len([cand for cand in cands if cand.atomicity == "composite"]),
        "non_skill_candidates": len([cand for cand in cands if cand.atomicity == "non_skill"]),
        "auto_accepted": len([cand for cand in resolved_candidates if not cand.council_ran and cand.decision == "accepted"]),
        "sent_to_council": len([cand for cand in resolved_candidates if cand.council_ran]),
        "accepted_after_council": len([cand for cand in resolved_candidates if cand.council_ran and cand.decision == "accepted"]),
        "review_after_council": len([cand for cand in resolved_candidates if cand.council_ran and cand.decision == "needs_review"]),
        "needs_review_total": len([cand for cand in cands if cand.decision == "needs_review"]),
        "accepted_total": len([cand for cand in resolved_candidates if cand.decision == "accepted"]),
        "matched_total": len([cand for cand in resolved_candidates if cand.resolution == "matched"]),
        "alias_total": len([cand for cand in resolved_candidates if cand.resolution == "alias"]),
        "fuzzy_total": len([cand for cand in resolved_candidates if cand.resolution == "fuzzy"]),
        "new_total": len([cand for cand in resolved_candidates if cand.resolution == "new"]),
    }


def run(brief: str, repo: CatalogRepo) -> tuple[dict, list[Evidence], list[SkillCandidate]]:
    spec = decompose(brief)
    evidence = gather_evidence(spec["sub_queries"])
    cands = atomize_candidates(synthesize(evidence, spec))
    resolve_candidates(cands, evidence, repo)
    run_council(cands)
    triage_candidates(cands)
    return spec, evidence, cands
