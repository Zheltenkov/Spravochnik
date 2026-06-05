"""Стадия 1->2: бриф -> навыки-кандидаты справочника.

Новый порядок экономит внешний поиск: сначала делаем draft skills из брифа,
резолвим их против канона, затем запускаем grounded-поиск только для серой зоны.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from datetime import date
from datetime import UTC, datetime, timedelta
from . import config, llm
from . import stage_atomize
from . import stage_normalize
from . import language
from .models import BLOOM, Evidence, IndicatorSpec, SkillCandidate
from .catalog_repo import CatalogRepo
from .skill_names import canonicalize_skill_name

RUSSIAN_OUTPUT_RULE = (
    "Все поля name, group, coverage_area, rationale и тексты индикаторов пиши на русском языке. "
    "Сохраняй на английском только общепринятые технические термины и аббревиатуры: MVP, API, REST, SQL, CI/CD, LLM, SLA, Git, Docker, OKR, unit economics, human-in-the-loop."
)


_BLOOM_BY_SCORE = {score: bloom for bloom, score in BLOOM.items()}
_HIGH_BLOOM_SIGNAL = re.compile(
    r"\b("
    r"созда(е|ё|й|т|ть)|разработ|спроект|собир|постро|сгенерир|"
    r"оцен|валидир|обоснов|выбер|защит"
    r")",
    re.IGNORECASE,
)
_ROUTINE_ACTION_SIGNAL = re.compile(
    r"\b("
    r"сформулир|формулир|определ|провед|настро|выяв|провер|"
    r"использ|примен|опис|фиксир|состав"
    r")",
    re.IGNORECASE,
)


def _seniority_bloom_ceiling(spec: dict[str, object] | None) -> str | None:
    seniority = str((spec or {}).get("seniority") or (spec or {}).get("target_seniority") or "").strip().casefold()
    if not seniority:
        return None
    if seniority in config.UP_MAX_BLOOM_BY_SENIORITY:
        return config.UP_MAX_BLOOM_BY_SENIORITY[seniority]
    for key, ceiling in config.UP_MAX_BLOOM_BY_SENIORITY.items():
        if key and key in seniority:
            return ceiling
    return None


def _clamp_bloom_for_audience(level: str, spec: dict[str, object] | None, text: str | None = None) -> str:
    ceiling = _seniority_bloom_ceiling(spec)
    if not ceiling or ceiling not in BLOOM or BLOOM[level] <= BLOOM[ceiling]:
        return level
    source_text = text or ""
    has_high_signal = bool(_HIGH_BLOOM_SIGNAL.search(source_text))
    has_routine_signal = bool(_ROUTINE_ACTION_SIGNAL.search(source_text))
    if not has_high_signal or has_routine_signal:
        return _BLOOM_BY_SCORE[BLOOM[ceiling]]
    return level


def normalize_bloom(value: str | None, spec: dict[str, object] | None = None, text: str | None = None) -> str:
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
    normalized = mapping.get(key, "understand")
    return _clamp_bloom_for_audience(normalized, spec, text)


def _normalized_spec(raw: dict[str, object]) -> dict[str, object]:
    role = str(raw.get("target_role") or raw.get("role") or "").strip()
    seniority = str(raw.get("target_seniority") or raw.get("seniority") or "").strip()
    domain = str(raw.get("domain") or "").strip()
    artifact_type = str(raw.get("artifact_type") or "learner_brief").strip() or "learner_brief"
    operator_role = str(raw.get("operator_role") or "").strip() or None
    program_goal = str(raw.get("program_goal") or "").strip()
    must_include_areas = [
        str(item).strip()
        for item in (raw.get("must_include_areas") or [])
        if str(item).strip()
    ]
    sub_queries = []
    seen_queries: set[str] = set()
    for item in (raw.get("sub_queries") or []):
        query = str(item).strip()
        norm = query.casefold()
        if not query or norm in seen_queries:
            continue
        seen_queries.add(norm)
        sub_queries.append(query)
    return {
        "artifact_type": artifact_type,
        "role": role,
        "seniority": seniority,
        "domain": domain,
        "operator_role": operator_role,
        "program_goal": program_goal,
        "must_include_areas": [language.localize_area_label(area) for area in must_include_areas],
        "sub_queries": sub_queries,
    }


def _mock_program_brief_spec() -> dict[str, object]:
    return {
        "artifact_type": "program_brief",
        "role": "Технологический предприниматель",
        "seniority": "начинающий",
        "domain": "технологическое предпринимательство / цифровые продукты / AI-assisted product building",
        "operator_role": "Дизайнер образовательной программы",
        "program_goal": "Подготовить начинающего технологического предпринимателя к запуску цифрового продукта как бизнеса.",
        "must_include_areas": [
            "Выявление проблемы клиента и customer discovery",
            "Проверка продуктовых гипотез",
            "Сегментация клиентов и ценностное предложение",
            "Определение границ MVP и продуктовая приоритизация",
            "AI-assisted разработка и архитектурное мышление",
            "Инженерная дисциплина: репозиторий, CI, тесты, релизы",
            "Инфраструктура: деплой, observability, backup, incidents",
            "AI-workflows и автоматизация бизнес-процессов",
            "Маркетинг технологического продукта",
            "Продажи и монетизация",
            "Поддержка пользователей и feedback loop",
            "Правовые, финансовые и административные основы",
            "Стратегия, управление рисками и цели",
            "Контроль качества AI, безопасность и human-in-the-loop",
        ],
        "sub_queries": [
            "Навыки выпускника для customer discovery, problem framing, JTBD и проверки гипотез в технологическом предпринимательстве",
            "Навыки выпускника для определения MVP, продуктовой приоритизации и AI-assisted разработки цифрового продукта",
            "Навыки выпускника для go-to-market: позиционирование, каналы привлечения, продажи и монетизация технологического продукта",
            "Навыки выпускника для инфраструктуры продукта, поддержки пользователей, observability, AI quality control и risk management",
        ],
    }


_PROGRAM_ARTIFACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bкогорт"), "Описывает формирование когорты, а не skill выпускника."),
    (re.compile(r"\bкритери(и|я)\s+отбор"), "Описывает правила набора в программу, а не skill выпускника."),
    (re.compile(r"\bучастник(ов|а|и)?\b"), "Описывает управление участниками программы, а не skill выпускника."),
    (re.compile(r"\bпреподавател"), "Описывает ресурс программы, а не skill выпускника."),
    (re.compile(r"\bнаставник|\bментор"), "Описывает состав команды сопровождения программы, а не learner-skill."),
    (re.compile(r"\bюрист|\bмаркетолог"), "Описывает staffing/outsourcing решение программы, а не learner-skill."),
    (re.compile(r"\bфаундер"), "Описывает организационную модель команды, а не наблюдаемый skill."),
    (re.compile(r"\bразмер\s+команд|\bчисленност"), "Описывает оргдизайн команды/потока, а не канонический skill."),
]

_COVERAGE_STOPWORDS = {
    "и", "или", "для", "по", "с", "на", "в", "из", "к", "от", "как", "а", "не", "это",
    "the", "and", "or", "of", "to", "with", "in",
    "навыки", "умения", "компетенции", "область", "области", "контур", "базовый", "базовая",
    "минимальный", "ключевой", "ключевые", "цифрового", "цифровых", "продукта", "продуктов",
    "продукт", "program", "brief", "learner", "graduate", "outcomes",
}


def _reclassify_program_artifacts(cands: list[SkillCandidate], spec: dict[str, object]) -> None:
    if str(spec.get("artifact_type") or "").strip() not in {"program_brief", "mixed"}:
        return
    for cand in cands:
        text = f"{cand.name} {cand.group}".casefold()
        for pattern, rationale in _PROGRAM_ARTIFACT_PATTERNS:
            if pattern.search(text):
                cand.entity_type = "curriculum_section"
                cand.atomicity = "non_skill"
                cand.decision = "needs_review"
                cand.reasons = ["non_skill:curriculum_section"]
                cand.atomize_rationale = rationale
                break


def _norm_tokens(value: str) -> set[str]:
    # Грубая нормализация нужна только для coverage-аудита и не влияет на канонические имена.
    tokens = {
        token
        for token in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9\-/]{3,}", value.casefold().replace("ё", "е"))
        if token not in _COVERAGE_STOPWORDS
    }
    return tokens


def _candidate_text(candidate: SkillCandidate) -> str:
    indicator_text = " ".join(indicator.text for indicator in candidate.indicators)
    return " ".join(
        part for part in [candidate.name, candidate.group, candidate.coverage_area or "", indicator_text] if part
    )


def _lexical_overlap(left: str, right: str) -> float:
    left_tokens = _norm_tokens(left)
    right_tokens = _norm_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    return len(intersection) / max(min(len(left_tokens), len(right_tokens)), 1)


def _build_coverage_audit(
    spec: dict[str, object],
    cands: list[SkillCandidate],
    coverage_rows: list[dict[str, object]] | None = None,
    compaction_events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    # Coverage нужен как продуктовый сигнал: видно, какие обязательные области закрыты, а какие выпали.
    areas = [str(item).strip() for item in spec.get("must_include_areas") or [] if str(item).strip()]
    if not areas:
        return {"covered_count": 0, "partial_count": 0, "uncovered_count": 0, "rows": []}

    skills = [cand for cand in cands if cand.entity_type == "skill" and cand.atomicity == "atomic"]
    coverage_index: dict[str, dict[str, object]] = {}
    for row in coverage_rows or []:
        area = str(row.get("area") or "").strip()
        if not area:
            continue
        coverage_index[area] = {
            "status": str(row.get("status") or "").strip() or "uncovered",
            "rationale": str(row.get("rationale") or "").strip(),
            "candidate_names": [str(name).strip() for name in row.get("candidate_names") or [] if str(name).strip()],
        }
    dropped_by_area: dict[str, list[str]] = {}
    cap_reason_by_area: dict[str, str] = {}
    for event in compaction_events or []:
        area = str(event.get("coverage_area") or "").strip()
        reason = str(event.get("reason") or "").strip()
        dropped_names = [str(name).strip() for name in event.get("dropped_names") or [] if str(name).strip()]
        if not area or not dropped_names:
            continue
        dropped_by_area.setdefault(area, []).extend(dropped_names)
        if reason.startswith("program_brief_area_cap"):
            cap_reason_by_area[area] = reason

    audit_rows: list[dict[str, object]] = []
    covered_count = 0
    partial_count = 0
    uncovered_count = 0
    for area in areas:
        explicit = [cand for cand in skills if (cand.coverage_area or "").strip() == area]
        if explicit:
            status = "covered"
            candidate_names = [cand.name for cand in explicit]
            rationale = "Покрыто кандидатами, явно привязанными к области."
        else:
            lexical = [cand for cand in skills if _lexical_overlap(area, _candidate_text(cand)) >= 0.34]
            if lexical:
                status = "partial"
                candidate_names = [cand.name for cand in lexical]
                rationale = "Найдено только частичное лексическое пересечение с кандидатами."
            else:
                status = "uncovered"
                candidate_names = []
                rationale = "Для области не найдено ни одного кандидата."

        if area in coverage_index:
            status = coverage_index[area]["status"] or status
            candidate_names = coverage_index[area]["candidate_names"] or candidate_names
            rationale = coverage_index[area]["rationale"] or rationale

        dropped_names = list(dict.fromkeys(dropped_by_area.get(area, [])))
        if dropped_names and area in cap_reason_by_area and status == "covered":
            status = "partial"
            rationale = (
                f"Область покрыта не полностью: часть кандидатов скрыта правилом {cap_reason_by_area[area]}. "
                "Нужно проверить, не являются ли они важными поднавыками."
            )

        if status == "covered":
            covered_count += 1
        elif status == "partial":
            partial_count += 1
        else:
            uncovered_count += 1

        audit_rows.append(
            {
                "area": area,
                "status": status,
                "candidate_names": candidate_names,
                "dropped_candidate_names": dropped_names,
                "rationale": rationale,
            }
        )

    return {
        "covered_count": covered_count,
        "partial_count": partial_count,
        "uncovered_count": uncovered_count,
        "rows": audit_rows,
    }


def build_coverage_audit(
    spec: dict[str, object],
    cands: list[SkillCandidate],
    coverage_rows: list[dict[str, object]] | None = None,
    normalize_report: dict[str, object] | None = None,
) -> dict[str, object]:
    compaction_events = []
    if isinstance(normalize_report, dict) and isinstance(normalize_report.get("compaction_events"), list):
        compaction_events = normalize_report["compaction_events"]
    return _build_coverage_audit(spec, cands, coverage_rows, compaction_events)


# --------- decompose ---------
def decompose(brief: str) -> dict:
    if config.USE_LIVE:
        sys = (
            "Ты анализируешь бриф для построения skill portrait. "
            "Если бриф описывает образовательную программу, курс, ветку, паспорт программы или ТЗ на продукт обучения, "
            "то role/seniority должны описывать выпускника/learner после завершения программы, а не автора, методолога, дизайнера программы или команду запуска. "
            "Отдельно выдели operator_role, если в тексте есть роль того, кто проектирует/запускает программу. "
            "Верни только JSON с полями: "
            "artifact_type ('learner_brief'|'program_brief'|'mixed'), "
            "role, seniority, domain, operator_role, program_goal, "
            "must_include_areas (list 8-16 обязательных областей компетенций выпускника), "
            "sub_queries (list 4-6 поисковых запросов только про learner skills и graduate outcomes). "
            "Запрещено заполнять sub_queries вопросами про размер когорты, бюджет программы, staffing, загрузку преподавателей, ресурсы команды запуска, KPI самой программы. "
            "Нужно вытаскивать skills выпускника, а не операционные решения по запуску программы."
        )
        raw = json.loads(llm.content(llm.chat(
            config.MODEL_PLAN,
            [{"role": "system", "content": sys}, {"role": "user", "content": brief}],
            json_mode=True,
        )))
        return _normalized_spec(raw)
    brief_lower = brief.lower()
    if "предпринимател" in brief_lower or "стартап" in brief_lower or "тз на продукт" in brief_lower:
        return _mock_program_brief_spec()
    return _normalized_spec(
        {
            "artifact_type": "learner_brief",
            "role": "Backend разработчик на Python",
            "seniority": "junior",
            "domain": "финтех",
            "operator_role": None,
            "program_goal": "",
            "must_include_areas": [
                "Python backend development",
                "Работа с БД и SQL",
                "REST API",
                "Очереди сообщений",
                "Docker",
            ],
            "sub_queries": [
                "junior backend требования",
                "работа с БД и SQL",
                "REST API",
                "очереди сообщений",
                "контейнеризация Docker",
            ],
        }
    )


def _normalize_evidence_query(query: str) -> str:
    return " ".join(query.casefold().replace("ё", "е").split())


def _evidence_cache_key(query: str) -> str:
    return hashlib.sha256(_normalize_evidence_query(query).encode("utf-8")).hexdigest()


def ensure_evidence_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_query_cache (
            cache_key TEXT PRIMARY KEY,
            normalized_query TEXT NOT NULL,
            query TEXT NOT NULL,
            model TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_query_cache_updated ON evidence_query_cache(updated_at)")
    conn.commit()


def _load_cached_search(cache_conn: sqlite3.Connection | None, query: str) -> list[dict] | None:
    if cache_conn is None:
        return None
    ensure_evidence_cache_table(cache_conn)
    row = cache_conn.execute(
        "SELECT response_json, updated_at FROM evidence_query_cache WHERE cache_key = ? AND model = ?",
        (_evidence_cache_key(query), config.MODEL_SEARCH),
    ).fetchone()
    if not row:
        return None
    try:
        updated_at = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - updated_at > timedelta(days=config.EVIDENCE_CACHE_TTL_DAYS):
        return None
    try:
        payload = json.loads(str(row["response_json"]))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, list) else None


def _store_cached_search(cache_conn: sqlite3.Connection | None, query: str, items: list[dict]) -> None:
    if cache_conn is None:
        return
    ensure_evidence_cache_table(cache_conn)
    now = datetime.now(UTC).isoformat()
    cache_conn.execute(
        """
        INSERT INTO evidence_query_cache(cache_key, normalized_query, query, model, response_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            normalized_query = excluded.normalized_query,
            query = excluded.query,
            model = excluded.model,
            response_json = excluded.response_json,
            updated_at = excluded.updated_at
        """,
        (
            _evidence_cache_key(query),
            _normalize_evidence_query(query),
            query,
            config.MODEL_SEARCH,
            json.dumps(items, ensure_ascii=False),
            now,
            now,
        ),
    )
    cache_conn.commit()


# --------- grounded-поиск -> evidence ---------
def search(query: str, cache_conn: sqlite3.Connection | None = None) -> list[dict]:
    cached = _load_cached_search(cache_conn, query)
    if cached is not None:
        return cached
    if config.USE_LIVE:
        sys = (
            "Найди подтверждающие источники по навыкам. "
            "Верни компактный JSON-массив объектов {claim, source_type, url, snippet}. "
            "source_type: vacancy|framework|syllabus|other. "
            "snippet должен быть коротким, без длинных цитат."
        )
        try:
            resp = llm.chat(
                config.MODEL_SEARCH,
                [{"role": "system", "content": sys}, {"role": "user", "content": query}],
                max_tokens=config.MODEL_SEARCH_MAX_TOKENS,
            )
            items = json.loads(llm.content(resp))
        except Exception:
            items = []
        cits = llm.citations(resp) if items else []
        for it in items:
            it.setdefault("url", cits[0] if cits else "")
            it.setdefault("snippet", "")
            it.setdefault("retrieved_at", date.today().isoformat())
        _store_cached_search(cache_conn, query, items)
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
    _store_cached_search(cache_conn, query, out)
    return out


def gather_evidence(sub_queries: list[str], cache_conn: sqlite3.Connection | None = None) -> list[Evidence]:
    ev, n = [], 0
    for q in sub_queries:
        for h in search(q, cache_conn=cache_conn):
            n += 1
            ev.append(Evidence(id=f"E{n:02d}", **{k: h[k] for k in ("claim", "source_type", "url", "snippet", "retrieved_at")}))
    # дедуп по (claim,url)
    seen, out = set(), []
    for e in ev:
        k = (e.claim.lower(), e.url)
        if k not in seen:
            seen.add(k); out.append(e)
    return out


def _candidate_source_text(candidate: SkillCandidate) -> str:
    parts = [
        candidate.name,
        candidate.group,
        candidate.coverage_area or "",
        " ".join(candidate.tools),
        " ".join(indicator.text for indicator in candidate.indicators),
    ]
    return " ".join(part for part in parts if part)


def _localize_candidate(candidate: SkillCandidate) -> SkillCandidate:
    """Normalize catalog-facing labels to Russian without touching technical terms."""
    localized_name = language.localize_skill_label(candidate.name)
    if localized_name and localized_name != candidate.name:
        candidate.source_name = candidate.source_name or candidate.name
        candidate.name = localized_name
    candidate.group = language.localize_group_label(candidate.group) or candidate.group
    if candidate.coverage_area:
        candidate.coverage_area = language.localize_area_label(candidate.coverage_area) or candidate.coverage_area
    return candidate


def synthesize_draft_from_brief(brief: str, spec: dict) -> tuple[list[SkillCandidate], dict[str, object] | None]:
    """Генерирует первичный shortlist из самого брифа без внешнего поиска.

    Этот draft нужен для дешёвого pre-match against canon: если навык уже есть в каталоге,
    Perplexity для него не вызывается.
    """
    if config.USE_LIVE:
        spec_context = {
            "artifact_type": spec.get("artifact_type"),
            "target_role": spec.get("role"),
            "target_seniority": spec.get("seniority"),
            "domain": spec.get("domain"),
            "operator_role": spec.get("operator_role"),
            "program_goal": spec.get("program_goal"),
            "must_include_areas": spec.get("must_include_areas", []),
        }
        if str(spec.get("artifact_type") or "").strip() in {"program_brief", "mixed"}:
            sys = (
                "Ты строишь первичный skill portrait выпускника образовательной программы только по тексту брифа, без внешнего поиска. "
                + RUSSIAN_OUTPUT_RULE
                + " "
                "Работай coverage-first по must_include_areas. "
                "Верни только строгий JSON вида "
                "{coverage:[{area,status,rationale,candidate_names}],"
                "candidates:[{name,group,coverage_area,indicators:[{text,bloom}],tools}]}. "
                "status в coverage: covered|partial|uncovered. "
                "Кандидаты должны быть learner-skills/graduate outcomes, а не роли, staffing, бюджет, ресурсы программы или решения команды запуска. "
                "На одну область дай 1-2 наиболее важных skill-кандидата. "
                "Название формулируй как наблюдаемый навык/action."
            )
        else:
            sys = (
                "Ты строишь первичный skill portrait обучаемого только по тексту брифа, без внешнего поиска. "
                + RUSSIAN_OUTPUT_RULE
                + " "
                "Верни строгий JSON {candidates:[{name,group,coverage_area,indicators:[{text,bloom}],tools}]}. "
                "Извлекай только learner-skills и graduate outcomes. Название формулируй как skill/action."
            )
        data = json.loads(llm.content(llm.chat(
            config.MODEL_PLAN,
            [
                {"role": "system", "content": sys},
                {"role": "user", "content": json.dumps({"spec": spec_context, "brief": brief}, ensure_ascii=False)},
            ],
            json_mode=True,
        )))
        out: list[SkillCandidate] = []
        for i, it in enumerate(data.get("candidates", []), 1):
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            out.append(
                _localize_candidate(
                    SkillCandidate(
                        tmp_id=f"C{i:02d}",
                        name=name,
                        group=str(it.get("group") or "").strip(),
                        coverage_area=str(it.get("coverage_area") or "").strip() or None,
                        indicators=[
                            IndicatorSpec(text=ind["text"], bloom=normalize_bloom(ind.get("bloom"), spec, ind.get("text")))
                            for ind in it.get("indicators", [])
                            if ind.get("text")
                        ],
                        tools=[str(tool).strip() for tool in it.get("tools", []) if str(tool).strip()],
                        evidence_ids=[],
                    )
                )
            )
        return out, _build_coverage_audit(spec, out, data.get("coverage"))

    # Offline/mock режим: сохраняем существующие демо-кандидаты, но без обязательного evidence.
    out, coverage = synthesize_with_coverage([], spec)
    if out:
        return out, coverage
    areas = [str(area).strip() for area in spec.get("must_include_areas") or [] if str(area).strip()]
    fallback: list[SkillCandidate] = []
    for i, area in enumerate(areas[:8], 1):
        fallback.append(
            _localize_candidate(
                SkillCandidate(
                    tmp_id=f"C{i:02d}",
                    name=area,
                    group=str(spec.get("domain") or ""),
                    coverage_area=area,
                    indicators=[IndicatorSpec(text=f"Применяет навык в области: {area}", bloom="apply")],
                    tools=[],
                    evidence_ids=[],
                )
            )
        )
    return fallback, _build_coverage_audit(spec, fallback)


def _needs_evidence_enrichment(candidate: SkillCandidate) -> bool:
    if not _is_for_resolve(candidate):
        return False
    if candidate.resolution in {"matched", "alias"} and candidate.confidence >= config.TAU_CONFIDENCE:
        return False
    return candidate.resolution in {"new", "fuzzy"} or candidate.confidence < config.TAU_CONFIDENCE


def select_evidence_enrichment_candidates(cands: list[SkillCandidate]) -> list[SkillCandidate]:
    return [cand for cand in cands if _needs_evidence_enrichment(cand)]


def gather_evidence_for_gray_zone(
    cands: list[SkillCandidate],
    spec: dict,
    cache_conn: sqlite3.Connection | None = None,
) -> list[Evidence]:
    """Ищет evidence только для кандидатов, которые не закрылись каноном."""
    grouped: dict[str, list[SkillCandidate]] = {}
    for cand in cands:
        if not _needs_evidence_enrichment(cand):
            continue
        key = (cand.coverage_area or cand.group or cand.name).strip()
        if not key:
            key = cand.name
        grouped.setdefault(key, []).append(cand)

    evidence: list[Evidence] = []
    seen: dict[tuple[str, str], str] = {}
    role = str(spec.get("role") or "").strip()
    domain = str(spec.get("domain") or "").strip()
    max_queries = max(config.GRAY_SEARCH_MAX_QUERIES, 0)
    for group_index, (area, area_candidates) in enumerate(grouped.items()):
        if group_index >= max_queries:
            break
        skill_names = ", ".join(candidate.name for candidate in area_candidates[:4])
        query = (
            f"Навыки выпускника для роли {role} в домене {domain}. "
            f"Область: {area}. Кандидаты: {skill_names}. "
            "Найди подтверждающие frameworks, syllabus или вакансии."
        )
        group_evidence_ids: list[str] = []
        for hit in search(query, cache_conn=cache_conn):
            key = (str(hit.get("claim", "")).casefold(), str(hit.get("url", "")))
            evidence_id = seen.get(key)
            if evidence_id is None:
                evidence_id = f"E{len(evidence) + 1:02d}"
                seen[key] = evidence_id
                evidence.append(Evidence(
                    id=evidence_id,
                    **{k: hit[k] for k in ("claim", "source_type", "url", "snippet", "retrieved_at")},
                ))
            group_evidence_ids.append(evidence_id)
        if not group_evidence_ids:
            continue
        for cand in area_candidates:
            cand.evidence_ids = list(dict.fromkeys([*cand.evidence_ids, *group_evidence_ids]))
    return evidence


# --------- синтез кандидатов (с Блумом и инструментами) ---------
def synthesize_with_coverage(evidence: list[Evidence], spec: dict) -> tuple[list[SkillCandidate], dict[str, object] | None]:
    ev_ids = [e.id for e in evidence]
    if config.USE_LIVE:
        cl = [{"id": e.id, "claim": e.claim, "type": e.source_type} for e in evidence]
        spec_context = {
            "artifact_type": spec.get("artifact_type"),
            "target_role": spec.get("role"),
            "target_seniority": spec.get("seniority"),
            "domain": spec.get("domain"),
            "operator_role": spec.get("operator_role"),
            "program_goal": spec.get("program_goal"),
            "must_include_areas": spec.get("must_include_areas", []),
        }
        if str(spec.get("artifact_type") or "").strip() in {"program_brief", "mixed"}:
            sys = (
                "Ты строишь skill portrait выпускника образовательной программы. "
                + RUSSIAN_OUTPUT_RULE
                + " "
                "Работай coverage-first: сначала посмотри на must_include_areas, затем попытайся закрыть их evidence. "
                "Верни только строгий JSON вида "
                "{coverage:[{area,status,rationale,candidate_names,evidence_ids}],"
                "candidates:[{name,group,coverage_area,indicators:[{text,bloom}],tools,evidence_ids}]}. "
                "status в coverage: covered|partial|uncovered. "
                "Кандидаты должны быть только learner-skills/graduate outcomes. "
                "Название кандидата формулируй как наблюдаемый навык или действие, а не как роль/должность человека. "
                "Хорошо: 'Проведение проблемных интервью', 'Настройка CI/CD', 'Определение границ MVP'. "
                "Плохо: 'Исследователь', 'Маркетолог', 'Стратег', 'Инженер MVP'. "
                "Не включай staffing decisions, преподавателей, бюджет, ресурсы программы, критерии набора, состав когорты, функции команды запуска, outsourcing-решения, роли операторов программы. "
                "Старайся не концентрироваться только в одной инженерной зоне: распределяй кандидатов по разным must_include_areas. "
                "На одну область давай 1-2 наиболее важных атомарных skill-кандидата, если evidence это поддерживает. "
                "evidence_ids разрешены только из предоставленного набора."
            )
        else:
            sys = (
                "Сгруппируй evidence в навыки-кандидаты выпускника. "
                + RUSSIAN_OUTPUT_RULE
                + " "
                "Строгий JSON {candidates:[{name,group,indicators:[{text,bloom}],tools,evidence_ids}]}. "
                "evidence_ids только из предоставленных. Навык без evidence не включай. "
                "Важное правило: извлекай только learner-skills и graduate outcomes. "
                "Название кандидата формулируй как skill/action, а не как роль или должность. "
                "Не включай staffing decisions, роли команды запуска программы, размер когорты, критерии набора, загрузку преподавателей, бюджет, ресурсы программы, outsourcing-решения. "
                "Если бриф про образовательную программу, ориентируйся на target_role и must_include_areas выпускника."
            )
        data = json.loads(llm.content(llm.chat(
            config.MODEL_PLAN,
            [{"role": "system", "content": sys}, {"role": "user", "content": json.dumps({"spec": spec_context, "evidence": cl}, ensure_ascii=False)}],
            json_mode=True,
        )))
        items = data.get("candidates", [])
        out = []
        for i, it in enumerate(items, 1):
            ids = [x for x in it.get("evidence_ids", []) if x in ev_ids]
            if not ids:
                continue
            out.append(
                _localize_candidate(
                    SkillCandidate(
                        tmp_id=f"C{i:02d}",
                        name=it["name"],
                        group=it.get("group", ""),
                        coverage_area=str(it.get("coverage_area") or "").strip() or None,
                        indicators=[
                            IndicatorSpec(text=ind["text"], bloom=normalize_bloom(ind.get("bloom"), spec, ind.get("text")))
                            for ind in it.get("indicators", [])
                            if ind.get("text")
                        ],
                        tools=it.get("tools", []),
                        evidence_ids=ids,
                    )
                )
            )
        coverage = _build_coverage_audit(spec, out, data.get("coverage"))
        return out, coverage
    # MOCK: реалистичные кандидаты под бриф (резолв пойдёт против реального каталога)
    role = str(spec.get("role", "")).lower()
    if "предпринимател" in role or "стартап" in role:
        proto = [
            ("Выявление проблемы клиента", "Customer Discovery", "Выявление проблемы клиента и customer discovery", [("Выявляет проблему клиента через интервью и наблюдение", "apply")], []),
            ("Проверка продуктовых гипотез", "Customer Discovery", "Проверка продуктовых гипотез", [("Проверяет гипотезы о проблеме и решении", "analyze")], []),
            ("Формулирование ценностного предложения", "Product Strategy", "Сегментация клиентов и ценностное предложение", [("Формулирует ценностное предложение для сегмента", "apply")], []),
            ("Определение границ MVP", "Product Strategy", "Определение границ MVP и продуктовая приоритизация", [("Определяет минимальный состав MVP", "apply")], []),
            ("Продуктовая приоритизация", "Product Strategy", "Определение границ MVP и продуктовая приоритизация", [("Приоритизирует backlog по ценности и рискам", "analyze")], []),
            ("AI-assisted разработка цифрового продукта", "Product Development", "AI-assisted разработка и архитектурное мышление", [("Использует AI для ускорения разработки продукта", "apply")], ["LLM", "IDE with AI"]),
            ("Настройка базовой инженерной дисциплины", "Engineering Delivery", "Инженерная дисциплина: репозиторий, CI, тесты, релизы", [("Ведет репозиторий, CI и тесты", "apply")], ["Git", "CI"]),
            ("Развертывание и наблюдаемость цифрового продукта", "Product Infrastructure", "Инфраструктура: деплой, observability, backup, incidents", [("Разворачивает сервис и настраивает observability", "apply")], ["Cloud", "Monitoring"]),
            ("Позиционирование и каналы привлечения", "Go-To-Market", "Маркетинг технологического продукта", [("Определяет позиционирование и каналы привлечения", "apply")], []),
            ("Построение модели монетизации", "Go-To-Market", "Продажи и монетизация", [("Формирует базовую модель монетизации продукта", "apply")], []),
            ("Поддержка пользователей и сбор обратной связи", "Customer Success", "Поддержка пользователей и feedback loop", [("Организует поддержку и feedback loop", "apply")], []),
            ("Контроль качества AI и безопасность", "Risk & Compliance", "Контроль качества AI, безопасность и human-in-the-loop", [("Проверяет результаты AI и управляет рисками безопасности", "analyze")], []),
        ]
        out = []
        for i, (name, grp, area, inds, tools) in enumerate(proto, 1):
            out.append(
                _localize_candidate(
                    SkillCandidate(
                        tmp_id=f"C{i:02d}",
                        name=name,
                        group=grp,
                        coverage_area=area,
                        indicators=[IndicatorSpec(text=text, bloom=bloom) for text, bloom in inds],
                        tools=tools,
                        evidence_ids=ev_ids[:2] or ev_ids[:1],
                    )
                )
            )
        return out, _build_coverage_audit(spec, out)

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
        out.append(
            _localize_candidate(
                SkillCandidate(
                    tmp_id=f"C{i:02d}",
                    name=name,
                    group=grp,
                    indicators=[IndicatorSpec(text=t, bloom=b) for t, b in inds],
                    tools=tools,
                    evidence_ids=ids,
                )
            )
        )
    return out, _build_coverage_audit(spec, out)


def synthesize(evidence: list[Evidence], spec: dict) -> list[SkillCandidate]:
    candidates, _coverage = synthesize_with_coverage(evidence, spec)
    return candidates


def _confidence(cand: SkillCandidate, evidence: list[Evidence]) -> float:
    evs = [e for e in evidence if e.id in cand.evidence_ids]
    fw = any(e.source_type in ("framework", "syllabus") for e in evs)
    evidence_confidence = min(min(0.5 + 0.2 * len(evs), 0.95) + (0.1 if fw else 0.0), 0.97)
    match_confidence = 0.0
    if cand.resolution in {"matched", "alias"}:
        match_confidence = 0.98
    elif cand.resolution == "fuzzy":
        match_confidence = min(max((cand.match_score or 0.0) / 100.0, 0.55), 0.93)
    elif cand.resolution == "new":
        match_confidence = 0.5
    return round(max(evidence_confidence if evs else 0.0, match_confidence), 2)


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


def atomize_candidates(cands: list[SkillCandidate], spec: dict | None = None) -> list[SkillCandidate]:
    if spec:
        _reclassify_program_artifacts(cands, spec)
    atomized = stage_atomize.run(cands)
    for candidate in atomized:
        _localize_candidate(candidate)
        if candidate.entity_type == "skill":
            canonical_name = canonicalize_skill_name(candidate.name)
            if canonical_name and canonical_name != candidate.name:
                candidate.source_name = candidate.source_name or candidate.name
                candidate.name = canonical_name
        if spec:
            candidate.indicators = [
                IndicatorSpec(text=indicator.text, bloom=normalize_bloom(indicator.bloom, spec, indicator.text))
                for indicator in candidate.indicators
            ]
    return atomized


def resolve_candidates(cands: list[SkillCandidate], evidence: list[Evidence], repo: CatalogRepo) -> None:
    for cand in cands:
        if not _is_for_resolve(cand):
            continue
        repo.resolve(cand)
        cand.confidence = _confidence(cand, evidence)


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


def _meets_auto_accept_policy(cand: SkillCandidate, spec: dict[str, object] | None = None) -> bool:
    artifact_type = str((spec or {}).get("artifact_type") or "").strip()
    # Новый skill в program_brief не публикуем автоматически: сначала нужен human check, иначе каталог быстро загрязняется.
    if (
        artifact_type in {"program_brief", "mixed"}
        and cand.resolution == "new"
        and not config.AUTO_ACCEPT_NEW_FOR_PROGRAM_BRIEF
    ):
        return False
    return (
        cand.council_agreement is not None
        and cand.confidence >= config.AUTO_ACCEPT_CONFIDENCE
        and cand.council_agreement >= config.AUTO_ACCEPT_COUNCIL_AGREEMENT
    )


def triage_candidates(cands: list[SkillCandidate], spec: dict[str, object] | None = None) -> None:
    artifact_type = str((spec or {}).get("artifact_type") or "").strip()
    for c in cands:
        if not _is_for_resolve(c):
            continue
        r = []
        n = len(set(c.evidence_ids))
        if c.resolution == "new":
            r.append("novel_skill")
            if artifact_type in {"program_brief", "mixed"} and not config.AUTO_ACCEPT_NEW_FOR_PROGRAM_BRIEF:
                r.append("program_brief_publication_guardrail")
        if c.resolution == "fuzzy":
            r.append("fuzzy_match_ambiguous")
        if c.confidence < config.TAU_CONFIDENCE:
            r.append("low_confidence")
        if n < config.MIN_SOURCES and c.resolution not in {"matched", "alias"}:
            r.append("single_source")
        if c.council_ran and c.council_agreement is not None and c.council_agreement < config.COUNCIL_AGREE_OK:
            r.append("council_split")
        if _meets_auto_accept_policy(c, spec):
            c.decision = "accepted"
            c.reasons = ["auto_accept_policy"]
            continue
        if c.resolution in {"matched", "alias"}:
            r = [reason for reason in r if reason not in {"novel_skill", "single_source", "fuzzy_match_ambiguous"}]
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
    cands, _coverage = synthesize_draft_from_brief(brief, spec)
    cands = atomize_candidates(cands, spec)
    cands, _normalize_report = stage_normalize.run(cands, spec)
    evidence: list[Evidence] = []
    resolve_candidates(cands, evidence, repo)
    evidence = gather_evidence_for_gray_zone(cands, spec)
    resolve_candidates(cands, evidence, repo)
    run_council(cands)
    triage_candidates(cands, spec)
    return spec, evidence, cands
