"""Стадия 1->2: бриф -> навыки-кандидаты справочника.

decompose -> grounded-поиск -> evidence -> синтез -> резолв (репозиторий)
-> жюри по серой зоне -> триаж.
"""
from __future__ import annotations
import json
from datetime import date
from . import config, llm
from .models import Evidence, IndicatorSpec, SkillCandidate
from .catalog_repo import CatalogRepo


# --------- decompose ---------
def decompose(brief: str) -> dict:
    if config.USE_LIVE:
        sys = ("Разложи бриф в JSON: role, seniority, domain, sub_queries(list 4-6). Только JSON.")
        return json.loads(llm.content(llm.chat(config.MODEL_PLAN,
            [{"role": "system", "content": sys}, {"role": "user", "content": brief}], json_mode=True)))
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
                indicators=[IndicatorSpec(**ind) for ind in it.get("indicators", [])],
                tools=it.get("tools", []), evidence_ids=ids))
        return out
    # MOCK: реалистичные кандидаты под бриф (резолв пойдёт против реального каталога)
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


def run(brief: str, repo: CatalogRepo) -> tuple[dict, list[Evidence], list[SkillCandidate]]:
    spec = decompose(brief)
    evidence = gather_evidence(spec["sub_queries"])
    cands = synthesize(evidence, spec)
    for c in cands:
        c.confidence = _confidence(c, evidence)
        repo.resolve(c)
    if config.USE_COUNCIL:
        for c in cands:
            if _needs_panel(c):
                votes = [_juror(m, c) for m in config.MODEL_PANEL]
                c.council_ran = True
                c.council_agreement = round(sum(votes) / len(votes), 2)
                c.confidence = round(0.6 * c.confidence + 0.4 * c.council_agreement, 2)
    for c in cands:
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
    return spec, evidence, cands
