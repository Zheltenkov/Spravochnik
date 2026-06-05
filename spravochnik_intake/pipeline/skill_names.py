"""Нормализация названий skill для канонической записи в справочник.

LLM часто возвращает action-фразы: "Провести интервью", "Настроить CI/CD".
Для каталога такие формулировки лучше хранить нейтрально: "Проведение интервью",
"Настройка CI/CD". Исходную action-формулировку сохраняем как alias/provenance.
"""
from __future__ import annotations

import re


_OBJECT_REWRITES = {
    "api-контракты для mvp": "API-контрактов для MVP",
    "api‑контракты для mvp": "API-контрактов для MVP",
    "а/b гипотезы": "A/B-гипотез",
    "a/b гипотезы": "A/B-гипотез",
    "глубинное интервью": "глубинных интервью",
    "ключевые сценарии использования": "ключевых сценариев использования",
    "маркетинговые каналы": "маркетинговых каналов",
    "паттерн интеграции llm": "паттерна интеграции LLM",
    "позиционирование": "позиционирования",
    "процесс triage": "процесса triage",
    "продуктовую страницу": "продуктовой страницы",
    "продуктовую стратегию": "продуктовой стратегии",
    "систему тикетов": "системы тикетов",
    "тарифную модель": "тарифной модели",
    "ценностное предложение": "ценностного предложения",
    "эксперименты для валидации гипотез": "экспериментов для валидации гипотез",
}

_ACTION_PREFIXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^провести\s+(.+)$", re.IGNORECASE), "Проведение"),
    (re.compile(r"^сформулировать\s+(.+)$", re.IGNORECASE), "Формулирование"),
    (re.compile(r"^формулировать\s+(.+)$", re.IGNORECASE), "Формулирование"),
    (re.compile(r"^определить\s+(.+)$", re.IGNORECASE), "Определение"),
    (re.compile(r"^выбрать\s+(.+)$", re.IGNORECASE), "Выбор"),
    (re.compile(r"^подготовить\s+(.+)$", re.IGNORECASE), "Подготовка"),
    (re.compile(r"^настроить\s+(.+)$", re.IGNORECASE), "Настройка"),
    (re.compile(r"^спроектировать\s+(.+)$", re.IGNORECASE), "Проектирование"),
    (re.compile(r"^разработать\s+(.+)$", re.IGNORECASE), "Разработка"),
    (re.compile(r"^описать\s+(.+)$", re.IGNORECASE), "Описание"),
    (re.compile(r"^оценивать\s+(.+)$", re.IGNORECASE), "Оценка"),
    (re.compile(r"^управлять\s+(.+)$", re.IGNORECASE), "Управление"),
    (re.compile(r"^приоритизировать\s+(.+)$", re.IGNORECASE), "Приоритизация"),
    (re.compile(r"^обеспечивать\s+(.+)$", re.IGNORECASE), "Обеспечение"),
]

_FRAGMENT_PREFIXES = {
    "сегментацию": "Сегментация",
    "анализ": "Анализ",
    "расчёт": "Расчёт",
    "расчет": "Расчёт",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("‑", "-").strip(" \t\r\n.,;:"))


def _rewrite_object(value: str) -> str:
    cleaned = _clean(value)
    key = cleaned.casefold().replace("ё", "е")
    return _OBJECT_REWRITES.get(key, cleaned)


def canonicalize_skill_name(name: str) -> str:
    """Возвращает нейтральное имя skill без повелительной/action-формы."""
    cleaned = _clean(name)
    if not cleaned:
        return cleaned

    for pattern, noun in _ACTION_PREFIXES:
        match = pattern.match(cleaned)
        if match:
            return _clean(f"{noun} {_rewrite_object(match.group(1))}")

    first, *rest = cleaned.split()
    fragment_noun = _FRAGMENT_PREFIXES.get(first.casefold().replace("ё", "е"))
    if fragment_noun:
        return _clean(" ".join([fragment_noun, *rest]))

    return cleaned[0].upper() + cleaned[1:]


def skill_name_variants(name: str | None) -> list[str]:
    """Даёт варианты имени для resolve: исходное + канонизированное без дублей."""
    variants: list[str] = []
    for candidate in [name or "", canonicalize_skill_name(name or "")]:
        cleaned = _clean(candidate)
        if cleaned and cleaned.casefold() not in {item.casefold() for item in variants}:
            variants.append(cleaned)
    return variants
