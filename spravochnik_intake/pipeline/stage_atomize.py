"""Стадия атомизации: между synthesize и resolve.

Каждый кандидат классифицируется:
  - atomic: один навык, пригодный для резолва и DAG
  - composite: широкая составная формулировка, разбивается на детей
  - non_skill: не навык, а рамка/блок программы
"""
from __future__ import annotations

import json
import re

from . import config, llm
from .models import IndicatorSpec, SkillCandidate

ATOMICITY_CONTRACT = (
    "Атомарный skill: один глагол + один объект; тестируется одним индикатором; "
    "не объединяет несколько практик, каналов или функций.\n"
    "Композитный кандидат: перечисляет несколько сущностей или действий через союзы/запятые; "
    "описывает функциональный блок, а не один навык.\n"
    "Non-skill: рамка программы, обзорная тема, блок функций, а не наблюдаемый навык.\n"
    "Все названия split_into.name, rationale и тексты индикаторов пиши на русском языке. "
    "Название skill пиши нейтрально для справочника, через отглагольное существительное: "
    "'Проведение интервью', 'Формулирование гипотезы', 'Настройка CI/CD'. "
    "Не пиши в повелительном наклонении или инфинитиве: плохо 'Провести интервью', 'Сформулировать гипотезу', 'Настроить CI/CD'. "
    "Артефакт без действия не является skill: 'лендинг', 'пробный доступ', 'ключевое сообщение' нужно либо превратить в наблюдаемое действие, либо признать non_skill. "
    "Не переводи только устойчивые технические термины и аббревиатуры: MVP, API, REST, SQL, CI/CD, LLM, SLA, Git, Docker, OKR, unit economics, human-in-the-loop."
)

GOLD_EXAMPLES = [
    {
        "name": "Формулирование и анализ проблемы",
        "verdict": "composite",
        "rationale": "несколько разных действий, набор практик discovery",
        "split_into": [
            {"name": "Выявление проблемы клиента", "indicators": [{"text": "Формулирует проблему словами клиента", "bloom": "apply"}]},
            {"name": "Problem framing / problem statement", "indicators": [{"text": "Составляет problem statement", "bloom": "apply"}]},
            {"name": "Формулирование задач в логике JTBD", "indicators": [{"text": "Описывает задачу по JTBD", "bloom": "apply"}]},
        ],
    },
    {
        "name": "Использование AI-инструментов в маркетинге",
        "verdict": "composite",
        "rationale": "функциональный блок; смешаны каналы, контент, аналитика, автоматизация",
        "split_into": [
            {"name": "Генерация маркетинговых материалов с AI", "indicators": [{"text": "Создаёт креативы с AI", "bloom": "apply"}]},
            {"name": "Использование AI для анализа каналов привлечения", "indicators": [{"text": "Анализирует каналы с AI", "bloom": "analyze"}]},
            {"name": "Использование AI для сегментации и маркетинговых гипотез", "indicators": [{"text": "Генерирует гипотезы с AI", "bloom": "apply"}]},
        ],
    },
    {
        "name": "Обзор ключевых функций организации",
        "verdict": "non_skill",
        "rationale": "рамка программы / обзорный модуль, а не наблюдаемое действие",
        "entity_type": "competency_block",
    },
    {
        "name": "Анализ и сегментация клиентов, метрики и продуктовый анализ",
        "verdict": "composite",
        "rationale": "минимум три отдельные сущности в одной записи",
        "split_into": [
            {"name": "Сегментация клиентов", "indicators": [{"text": "Сегментирует клиентскую базу", "bloom": "apply"}]},
            {"name": "Работа с продуктовыми метриками", "indicators": [{"text": "Считает продуктовые метрики", "bloom": "apply"}]},
            {"name": "Продуктовая аналитика", "indicators": [{"text": "Делает продуктовый анализ", "bloom": "analyze"}]},
        ],
    },
]


def prefilter(name: str) -> tuple[bool, str]:
    """Дешёвый rule-based фильтр для очевидно неатомарных формулировок."""
    lowered = name.lower().strip()
    if re.search(r"\s(и|или)\s", lowered):
        return True, "conjunction"
    if "," in name:
        return True, "commas_in_name"
    if len(name.split()) >= 6:
        return True, "name_too_long"
    if re.search(r"^(основные|основы|базовые|обзор|введение в)\b", lowered):
        return True, "overview_pattern"
    if re.search(r"-инструмент(ы|ов)\s+в\s+", lowered):
        return True, "tools_in_domain_pattern"
    return False, ""


def _call_live(cand: SkillCandidate) -> dict[str, object]:
    sys = (
        "Ты методолог справочника навыков. Реши, является ли кандидат атомарным skill, "
        "композитом или не-навыком. Контракт:\n"
        + ATOMICITY_CONTRACT
        + "\n\nВерни строгий JSON:\n"
        + '{"verdict":"atomic"|"composite"|"non_skill","rationale":"...","split_into":[{"name":"...","indicators":[{"text":"...","bloom":"apply"}]}],"entity_type":"competency_block"|"curriculum_section"}'
        + "\n\nПримеры решений методолога:\n"
        + json.dumps(GOLD_EXAMPLES, ensure_ascii=False, indent=2)
    )
    user = json.dumps(
        {
            "candidate": {
                "name": cand.name,
                "group": cand.group,
                "indicators": [indicator.model_dump() for indicator in cand.indicators],
            }
        },
        ensure_ascii=False,
    )
    response = llm.chat(
        config.MODEL_PLAN,
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        json_mode=True,
    )
    return json.loads(llm.content(response))


def _call_live_batch(cands: list[SkillCandidate]) -> dict[str, dict[str, object]]:
    sys = (
        "Ты методолог справочника навыков. Для каждого кандидата реши, является ли он атомарным skill, "
        "композитом или не-навыком. Контракт:\n"
        + ATOMICITY_CONTRACT
        + "\n\nВерни строгий JSON вида:\n"
        + '{"items":[{"id":"...","verdict":"atomic"|"composite"|"non_skill","rationale":"...","split_into":[{"name":"...","indicators":[{"text":"...","bloom":"apply"}]}],"entity_type":"competency_block"|"curriculum_section"}]}'
        + "\n\nПримеры решений методолога:\n"
        + json.dumps(GOLD_EXAMPLES, ensure_ascii=False, indent=2)
    )
    user = json.dumps(
        {
            "candidates": [
                {
                    "id": cand.tmp_id,
                    "name": cand.name,
                    "group": cand.group,
                    "indicators": [indicator.model_dump() for indicator in cand.indicators],
                }
                for cand in cands
            ]
        },
        ensure_ascii=False,
    )
    response = llm.chat(
        config.MODEL_PLAN,
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        json_mode=True,
    )
    data = json.loads(llm.content(response))
    decisions: dict[str, dict[str, object]] = {}
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if item_id:
            decisions[item_id] = item
    return decisions


def _call_mock(cand: SkillCandidate) -> dict[str, object]:
    normalized = cand.name.lower().strip()
    for example in GOLD_EXAMPLES:
        if example["name"].lower().strip() == normalized:
            return {key: value for key, value in example.items() if key != "name"}

    suspicious, reason = prefilter(cand.name)
    if reason == "overview_pattern":
        return {
            "verdict": "non_skill",
            "rationale": "обзорный или рамочный заголовок",
            "entity_type": "competency_block",
        }
    if suspicious and reason in {"conjunction", "commas_in_name", "tools_in_domain_pattern"}:
        parts = re.split(r"\s+и\s+|,\s*", cand.name, maxsplit=2)
        parts = [part.strip() for part in parts if part.strip()]
        if len(parts) >= 2:
            return {
                "verdict": "composite",
                "rationale": f"эвристика: {reason}",
                "split_into": [
                    {
                        "name": part,
                        "indicators": [{"text": f"Демонстрирует: {part.lower()}", "bloom": "apply"}],
                    }
                    for part in parts
                ],
            }
    return {"verdict": "atomic", "rationale": "по эвристике признаков композитности нет"}


def _rule_based_split(cand: SkillCandidate, reason: str) -> dict[str, object] | None:
    # Очевидные композиции лучше разбирать детерминированно, без лишнего LLM-вызова на каждый кандидат.
    if reason not in {"conjunction", "commas_in_name"}:
        return None
    parts = re.split(r"\s+\+\s+|\s+и\s+|,\s*", cand.name)
    parts = [part.strip(" -–—") for part in parts if part and part.strip(" -–—")]
    if len(parts) < 2:
        return None
    # Если хотя бы одна часть слишком короткая, это обычно перечисление объекта, а не набор самостоятельных skills.
    if any(len(part.split()) < 2 for part in parts):
        return None
    return {
        "verdict": "composite",
        "rationale": f"rule-based split: {reason}",
        "split_into": [
            {
                "name": part,
                "indicators": [{"text": f"Демонстрирует навык: {part.lower()}", "bloom": "apply"}],
            }
            for part in parts
        ],
    }


def atomize_one(cand: SkillCandidate) -> dict[str, object]:
    decision = _prefilter_decision(cand)
    if decision is not None:
        return decision
    return _call_live(cand) if config.USE_LIVE else _call_mock(cand)


def _prefilter_decision(cand: SkillCandidate) -> dict[str, object] | None:
    suspicious, reason = prefilter(cand.name)
    if not suspicious:
        return {"verdict": "atomic", "rationale": "rule-prefilter: признаков композитности нет"}
    if decision := _rule_based_split(cand, reason):
        return decision
    return None


def _child(parent: SkillCandidate, index: int, item: dict[str, object]) -> SkillCandidate:
    return SkillCandidate(
        tmp_id=f"{parent.tmp_id}.{index}",
        name=str(item["name"]),
        group=parent.group,
        coverage_area=parent.coverage_area,
        indicators=[IndicatorSpec(**indicator) for indicator in item.get("indicators", [])],
        tools=list(item.get("tools", parent.tools)),
        evidence_ids=list(parent.evidence_ids),
        entity_type="skill",
        atomicity="atomic",
        parent_tmp_id=parent.tmp_id,
    )


def run(cands: list[SkillCandidate]) -> list[SkillCandidate]:
    """Атомизирует список кандидатов, сохраняя parent для provenance."""
    out: list[SkillCandidate] = []
    prepared: list[tuple[SkillCandidate, dict[str, object] | None]] = []
    live_pending: list[SkillCandidate] = []
    for cand in cands:
        if cand.atomicity == "non_skill" and cand.entity_type != "skill" and cand.decision == "needs_review":
            prepared.append((cand, {"verdict": "passthrough", "rationale": cand.atomize_rationale}))
            continue
        decision = _prefilter_decision(cand)
        if decision is None and not config.USE_LIVE:
            decision = _call_mock(cand)
        if decision is None:
            live_pending.append(cand)
        prepared.append((cand, decision))

    live_decisions: dict[str, dict[str, object]] = {}
    if live_pending:
        try:
            live_decisions = _call_live_batch(live_pending)
        except Exception:
            live_decisions = {}

    for cand, prepared_decision in prepared:
        decision = prepared_decision
        if decision and decision.get("verdict") == "passthrough":
            out.append(cand)
            continue
        if decision is None:
            decision = live_decisions.get(cand.tmp_id)
        if decision is None:
            decision = _call_live(cand) if config.USE_LIVE else _call_mock(cand)
        verdict = str(decision.get("verdict", "atomic"))
        cand.atomize_rationale = str(decision.get("rationale", ""))
        if verdict == "atomic":
            cand.atomicity = "atomic"
            out.append(cand)
            continue
        if verdict == "non_skill":
            cand.atomicity = "non_skill"
            cand.entity_type = str(decision.get("entity_type", "competency_block"))
            cand.decision = "needs_review"
            cand.reasons = (cand.reasons or []) + [f"non_skill:{cand.entity_type}"]
            out.append(cand)
            continue
        if verdict == "composite":
            cand.atomicity = "composite"
            cand.decision = "superseded"
            cand.reasons = (cand.reasons or []) + ["composite_decomposed"]
            out.append(cand)
            for index, item in enumerate(decision.get("split_into", []), 1):
                out.append(_child(cand, index, item))
            continue
        cand.atomicity = "unknown"
        out.append(cand)
    return out
