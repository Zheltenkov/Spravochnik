"""LLM council for curriculum artifact template proposals.

This module is intentionally proposal-only: it can draft human-reviewable
templates, but it cannot promote them into the working curriculum template
catalog. Promotion remains an explicit methodologist action.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import config, llm

PROMPT_VERSION = "up-template-consilium-v1"
ALLOWED_ARTIFACT_FAMILIES = {"analysis", "document", "configuration", "design", "production", "practice"}
FALLBACK_FAMILY = "practice"


class TemplateConsiliumError(RuntimeError):
    """Raised when the council output cannot be safely converted to proposals."""


def _clean_text(value: object, *, max_len: int, default: str = "") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return default
    return text[:max_len].rstrip()


def _clean_multiline(value: object, *, max_len: int, default: str = "") -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return default
    return text[:max_len].rstrip()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^\wа-яА-ЯёЁ]+", "-", value.casefold(), flags=re.UNICODE).strip("-")
    return normalized or "item"


def _extract_json_object(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise TemplateConsiliumError("LLM response is not JSON") from None
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise TemplateConsiliumError("LLM response root must be an object")
    return data


def _repair_json_response(raw: str) -> dict[str, Any]:
    """Ask the same model to repair malformed JSON without changing content."""
    repair_messages = [
        {
            "role": "system",
            "content": (
                "Ты JSON repair tool. Верни только валидный JSON object. "
                "Не добавляй markdown. Не меняй смысл, названия, ids и поля. "
                "Если поле невозможно восстановить, оставь его пустой строкой или пустым массивом."
            ),
        },
        {
            "role": "user",
            "content": (
                "Исправь синтаксис JSON. Требуемый корневой формат: "
                "{\"proposals\": [...]}\n\n"
                + raw[:12000]
            ),
        },
    ]
    repaired = llm.chat(
        config.MODEL_TEMPLATE_COUNCIL,
        repair_messages,
        json_mode=True,
        timeout=config.UP_TEMPLATE_COUNCIL_TIMEOUT_SECONDS,
        max_tokens=config.MODEL_TEMPLATE_COUNCIL_MAX_TOKENS,
    )
    return _extract_json_object(llm.content(repaired))


def _brief_summary(brief: dict[str, object]) -> dict[str, object]:
    raw_text = _clean_multiline(brief.get("raw_text"), max_len=1800)
    return {
        "id": brief.get("id"),
        "role": _clean_text(brief.get("role"), max_len=120),
        "seniority": _clean_text(brief.get("seniority"), max_len=80),
        "domain": _clean_text(brief.get("domain"), max_len=120),
        "raw_text_excerpt": raw_text,
    }


def _system_prompt(max_proposals: int) -> str:
    return f"""
Ты методологический LLM-консилиум для системы генерации учебного плана.

Задача:
- предложить редактируемые ШАБЛОНЫ УП для принятого набора atomic skills;
- шаблон УП описывает проверяемый учебный артефакт проекта, а не конкретную строку УП;
- предложения попадают только в очередь методолога и НЕ становятся рабочими без ручного принятия.

Ключевые определения:
- coverage_area: смысловая область требований брифа;
- accepted skill: навык, уже подтверждённый методологически;
- artifact template: reusable правило, которое превращает группу skills в проектный артефакт;
- artifact_family: один из analysis, document, configuration, design, production, practice.

Методологическая политика:
1. Не используй локальные доменные архетипы и готовые названия из памяти. Нельзя навязывать Customer Discovery Sprint, GTM, Demo Day, DevOps и т.п., если это не следует из входного текста.
2. Все названия и описания должны быть на русском языке. Оставляй английскими только устойчивые технические термины: API, MVP, CI/CD, LLM, SLA, Git, Docker, unit economics, human-in-the-loop.
3. Название шаблона должно быть существительным/нейтральной формулировкой, не повелительным наклонением. Плохо: "Проведи интервью". Хорошо: "Отчёт клиентского исследования".
4. Один шаблон должен иметь один проверяемый артефакт: документ, отчёт, схема, конфигурация, рабочий прототип, процесс, пакет материалов.
5. Шаблон не должен быть слишком широким: если skills относятся к разным проверяемым артефактам, предложи отдельные шаблоны.
6. Шаблон не должен быть слишком узким: если 2-4 skills поддерживают один артефакт, объедини их.
7. Не придумывай новые skills. covered_skill_ids можно брать только из входного списка.
8. Не меняй смысл DAG и не предлагай prerequisites. Ты работаешь только с шаблонами проектных артефактов.
9. Не создавай требования к заданиям сверх артефакта и критериев проверки.
10. Максимум предложений: {max_proposals}.

Правила выбора artifact_family:
- analysis: исследование, доказательства, интервью, гипотезы, метрики, анализ рисков;
- document: юридические, финансовые, управленческие, позиционирующие или формальные документы;
- design: архитектура, структура решения, UX, roadmap, спецификация, границы MVP;
- configuration: настройка репозитория, инфраструктуры, CI/CD, мониторинга, окружений;
- production: рабочий продукт, автоматизированный workflow, эксплуатационный процесс;
- practice: коммуникация, поддержка, обратная связь, операционная практика.

Формат ответа:
Верни строго JSON object без markdown:
{{
  "proposals": [
    {{
      "scope_names": ["точное имя coverage_area из входа"],
      "title": "короткое название, до 64 символов",
      "artifact_family": "analysis|document|configuration|design|production|practice",
      "artifact_description": "что студент предъявляет как проверяемый артефакт",
      "project_name_pattern": "название проекта/занятия; можно использовать {{theme}}, {{skills}}, {{first_skill}}",
      "materials_pattern": "какие материалы нужны; можно использовать {{skills}}",
      "storytelling_pattern": "роль и контекст студента в проекте",
      "validation_criteria": "критерии проверки артефакта",
      "covered_skill_ids": [1, 2],
      "rationale": "почему этот шаблон методологически нужен",
      "confidence": 0.0
    }}
  ]
}}

Если область не образует проверяемый артефакт, не возвращай proposal по ней.
""".strip()


def build_messages(
    *,
    brief: dict[str, object],
    scope_groups: list[dict[str, object]],
    max_proposals: int,
) -> list[dict[str, str]]:
    payload = {
        "prompt_version": PROMPT_VERSION,
        "brief": _brief_summary(brief),
        "max_proposals": max_proposals,
        "artifact_families": sorted(ALLOWED_ARTIFACT_FAMILIES),
        "scope_groups": scope_groups,
    }
    return [
        {"role": "system", "content": _system_prompt(max_proposals)},
        {
            "role": "user",
            "content": (
                "Сформируй proposals шаблонов УП по входным данным. "
                "Соблюдай JSON-контракт и не используй skills/scopes вне входа.\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
            ),
        },
    ]


def validate_proposals(
    raw: dict[str, Any],
    *,
    scope_groups: list[dict[str, object]],
    max_proposals: int,
    source: str,
) -> list[dict[str, object]]:
    """Convert LLM JSON into safe proposal payloads.

    The validator is deliberately strict about ids and scopes. It may repair
    text fields, but it never lets the model invent catalog references.
    """
    proposals_raw = raw.get("proposals")
    if not isinstance(proposals_raw, list):
        raise TemplateConsiliumError("Missing proposals list")

    scope_to_ids: dict[str, set[int]] = {}
    id_to_name: dict[int, str] = {}
    for group in scope_groups:
        scope_name = _clean_text(group.get("scope_name"), max_len=240)
        if not scope_name:
            continue
        skills = group.get("skills") if isinstance(group.get("skills"), list) else []
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            try:
                skill_id = int(skill.get("id"))
            except (TypeError, ValueError):
                continue
            name = _clean_text(skill.get("name"), max_len=180)
            scope_to_ids.setdefault(scope_name, set()).add(skill_id)
            id_to_name[skill_id] = name

    validated: list[dict[str, object]] = []
    seen_codes: set[str] = set()
    for item in proposals_raw[: max_proposals * 2]:
        if not isinstance(item, dict):
            continue
        scope_names = [
            _clean_text(scope, max_len=240)
            for scope in (item.get("scope_names") if isinstance(item.get("scope_names"), list) else [])
        ]
        scope_names = [scope for scope in scope_names if scope in scope_to_ids]
        if not scope_names:
            continue

        allowed_ids = set().union(*(scope_to_ids[scope] for scope in scope_names))
        skill_ids: list[int] = []
        for raw_id in item.get("covered_skill_ids") if isinstance(item.get("covered_skill_ids"), list) else []:
            try:
                skill_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if skill_id in allowed_ids and skill_id not in skill_ids:
                skill_ids.append(skill_id)
        if not skill_ids:
            skill_ids = sorted(allowed_ids)
        if not skill_ids:
            continue

        family = _clean_text(item.get("artifact_family"), max_len=40).casefold()
        if family not in ALLOWED_ARTIFACT_FAMILIES:
            family = FALLBACK_FAMILY

        title = _clean_text(item.get("title"), max_len=80, default=f"Артефакт: {scope_names[0]}")
        description = _clean_multiline(
            item.get("artifact_description"),
            max_len=900,
            default=f"Проверяемый артефакт по области «{scope_names[0]}», покрывающий навыки {{skills}}.",
        )
        criteria = _clean_multiline(
            item.get("validation_criteria"),
            max_len=900,
            default="Артефакт предъявлен; решения обоснованы; навыки {skills} применены в проверяемом результате.",
        )
        code = f"proposal-{_slug('|'.join(scope_names))}"
        if code in seen_codes:
            continue
        seen_codes.add(code)
        validated.append(
            {
                "code": code,
                "title": title,
                "artifact_family": family,
                "scope_type": "coverage_area",
                "scope_names": scope_names,
                "artifact_description": description,
                "project_name_pattern": _clean_multiline(item.get("project_name_pattern"), max_len=500, default=title),
                "materials_pattern": _clean_multiline(
                    item.get("materials_pattern"),
                    max_len=900,
                    default="Бриф, рабочие материалы, шаблон артефакта, список навыков: {skills}.",
                ),
                "storytelling_pattern": _clean_multiline(
                    item.get("storytelling_pattern"),
                    max_len=900,
                    default="Студент действует в роли исполнителя проекта и предъявляет проверяемый результат.",
                ),
                "validation_criteria": criteria,
                "covered_skill_ids": skill_ids,
                "covered_skill_names": [id_to_name[skill_id] for skill_id in skill_ids if id_to_name.get(skill_id)],
                "rationale": _clean_multiline(
                    item.get("rationale"),
                    max_len=700,
                    default="Предложено LLM-консилиумом как проверяемый артефакт для accepted skills.",
                ),
                "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.65))),
                "source": source,
            }
        )
        if len(validated) >= max_proposals:
            break
    if not validated:
        raise TemplateConsiliumError("No valid proposals after validation")
    return validated


def propose(
    *,
    brief: dict[str, object],
    scope_groups: list[dict[str, object]],
    max_proposals: int,
) -> list[dict[str, object]]:
    messages = build_messages(brief=brief, scope_groups=scope_groups, max_proposals=max_proposals)
    response = llm.chat(
        config.MODEL_TEMPLATE_COUNCIL,
        messages,
        json_mode=True,
        timeout=config.UP_TEMPLATE_COUNCIL_TIMEOUT_SECONDS,
        max_tokens=config.MODEL_TEMPLATE_COUNCIL_MAX_TOKENS,
    )
    raw_content = llm.content(response) or ""
    if not raw_content.strip():
        raise TemplateConsiliumError("LLM returned empty content; increase MODEL_TEMPLATE_COUNCIL_MAX_TOKENS or reduce prompt")
    try:
        data = _extract_json_object(raw_content)
        source_suffix = PROMPT_VERSION
    except (json.JSONDecodeError, TemplateConsiliumError, TypeError):
        data = _repair_json_response(raw_content)
        source_suffix = f"{PROMPT_VERSION}:json_repair"
    source = f"llm_consilium:{config.MODEL_TEMPLATE_COUNCIL}:{PROMPT_VERSION}"
    if source_suffix != PROMPT_VERSION:
        source = f"llm_consilium:{config.MODEL_TEMPLATE_COUNCIL}:{source_suffix}"
    return validate_proposals(data, scope_groups=scope_groups, max_proposals=max_proposals, source=source)
