"""Language normalization helpers for catalog-facing labels.

The pipeline may receive English labels from LLMs or evidence sources. The
catalog and curriculum UI are Russian-first, while short technical terms such
as MVP, CI/CD, API, LLM, SLA and Git should stay unchanged.
"""
from __future__ import annotations

import re

_CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")


def _norm(value: str | None) -> str:
    text = (value or "").strip().casefold().replace("ё", "е")
    text = text.replace("&", " and ")
    text = re.sub(r"[\s_/]+", " ", text)
    text = re.sub(r"[^0-9a-zа-я+\-. ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def has_cyrillic(value: str | None) -> bool:
    return bool(_CYRILLIC_RE.search(value or ""))


_GROUP_ALIASES = {
    "ai and automation": "AI и автоматизация",
    "ai automation": "AI и автоматизация",
    "ai safety and qa": "Качество и безопасность AI",
    "architecture": "Архитектура",
    "customer discovery": "Исследование клиентов",
    "customer success": "Поддержка клиентов",
    "engineering delivery": "Инженерная поставка",
    "engineering discipline": "Инженерная дисциплина",
    "go-to-market": "Вывод продукта на рынок",
    "go to market": "Вывод продукта на рынок",
    "infrastructure and ops": "Инфраструктура и эксплуатация",
    "legal and admin": "Право и администрирование",
    "marketing": "Маркетинг",
    "mvp and roadmap": "MVP и дорожная карта",
    "positioning": "Позиционирование",
    "product development": "Разработка продукта",
    "product infrastructure": "Инфраструктура продукта",
    "product strategy": "Продуктовая стратегия",
    "research": "Исследования",
    "risk and compliance": "Риски и соответствие",
    "sales and monetization": "Продажи и монетизация",
    "strategy and governance": "Стратегия и управление",
    "support and feedback": "Поддержка и обратная связь",
    "theme": "Общее",
    "user research": "Исследование пользователей",
}


_AREA_ALIASES = {
    "ai assisted product building": "AI-assisted создание продукта",
    "ci cd pipelines and automated tests for releases": "CI/CD, автотесты и релизы",
    "customer discovery": "Исследование клиентов",
    "go to market": "Вывод продукта на рынок",
    "mvp scope": "Границы MVP",
    "product research and experimentation": "Продуктовые исследования и эксперименты",
    "scalability and ai components": "Масштабируемость и AI-компоненты",
    "user journeys and use cases": "Пользовательские сценарии",
}


_SKILL_ALIASES = {
    "automate deployment and document runbooks": "Автоматизация деплоя и описание runbook",
    "ci cd pipelines and automated tests for releases": "Настройка CI/CD и автотестов для релизов",
    "close the loop with users": "Замыкание feedback loop с пользователями",
    "conduct semi-structured customer interviews": "Проведение полуструктурированных клиентских интервью",
    "configure monitoring and alerts": "Настройка мониторинга и алертов",
    "construct a time-bound 3-month roadmap": "Составление дорожной карты на 3 месяца",
    "craft product positioning": "Формулирование позиционирования продукта",
    "create target personas": "Создание целевых персон",
    "define development goals": "Определение целей развития",
    "define testable hypotheses": "Формулирование проверяемых гипотез",
    "design an interview guide": "Подготовка гайда интервью",
    "design pricing": "Проектирование модели ценообразования",
    "document minimal feature set mvp scope": "Описание минимального набора функций MVP",
    "ensure data quality and human-in-the-loop checks": "Обеспечение качества данных и human-in-the-loop проверок",
    "identify automation opportunities with llms": "Выявление возможностей автоматизации с LLM",
    "identify jurisdictional requirements": "Определение правовых требований",
    "implement backups and incident response": "Настройка backup и реагирования на инциденты",
    "implement end-to-end automated workflow": "Внедрение end-to-end автоматизированного workflow",
    "implement lightweight mvp experiments": "Проведение легких MVP-экспериментов",
    "implement support channel and sla": "Настройка канала поддержки и SLA",
    "interpret experiment results and decide": "Интерпретация результатов экспериментов",
    "landing page and initial acquisition funnel": "Сборка лендинга и первичной воронки привлечения",
    "maintain minimal administrative checklist": "Ведение базового административного чек-листа",
    "maintain source control": "Ведение контроля версий",
    "map key user journeys use-cases": "Картирование пользовательских сценариев",
    "monitor and tune automation outcomes": "Мониторинг и настройка результатов автоматизации",
    "prepare basic legal documents": "Подготовка базовых юридических документов",
    "prioritize features using a framework": "Приоритизация функций по фреймворку",
    "produce a product architecture that balances simplicity": "Проектирование простой продуктовой архитектуры",
    "risk register and regular management rhythm okrs and roadmap reviews": "Ведение реестра рисков и управленческого ритма",
    "run review cadence and update roadmap": "Проведение ревью и обновление дорожной карты",
    "select validation metrics": "Выбор метрик валидации",
    "trial mechanics and basic unit economics to support early monetization": "Проектирование пробного запуска и базовой unit economics",
    "unique value proposition and core use-cases": "Формулирование ценностного предложения и базовых сценариев",
    "validate and monitor ai outputs": "Валидация и мониторинг AI-выводов",
}


def localize_group_label(value: str | None) -> str:
    label = (value or "").strip()
    if not label:
        return ""
    return _GROUP_ALIASES.get(_norm(label), label)


def localize_area_label(value: str | None) -> str:
    label = (value or "").strip()
    if not label:
        return ""
    normalized = _norm(label)
    if normalized in _AREA_ALIASES:
        return _AREA_ALIASES[normalized]
    if normalized in _GROUP_ALIASES:
        return _GROUP_ALIASES[normalized]
    return label


def localize_skill_label(value: str | None) -> str:
    label = (value or "").strip()
    if not label:
        return ""
    normalized = _norm(label)
    if not normalized or has_cyrillic(label):
        return label
    if normalized in _SKILL_ALIASES:
        return _SKILL_ALIASES[normalized]
    for source, target in _SKILL_ALIASES.items():
        if len(source) >= 18 and (source in normalized or normalized in source):
            return target
    return label
