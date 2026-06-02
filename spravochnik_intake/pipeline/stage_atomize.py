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
    "Non-skill: рамка программы, обзорная тема, блок функций, а не наблюдаемый навык."
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
        "name": "Основные бизнес-функции технологического стартапа",
        "verdict": "non_skill",
        "rationale": "рамка программы / обзорный модуль, а не действие",
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


def atomize_one(cand: SkillCandidate) -> dict[str, object]:
    suspicious, _reason = prefilter(cand.name)
    if not suspicious:
        return {"verdict": "atomic", "rationale": "rule-prefilter: признаков композитности нет"}
    return _call_live(cand) if config.USE_LIVE else _call_mock(cand)


def _child(parent: SkillCandidate, index: int, item: dict[str, object]) -> SkillCandidate:
    return SkillCandidate(
        tmp_id=f"{parent.tmp_id}.{index}",
        name=str(item["name"]),
        group=parent.group,
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
    for cand in cands:
        decision = atomize_one(cand)
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
