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
    "ключевое сообщение": "ключевого сообщения продукта",
    "метод проверки": "метода проверки",
    "метод проверки и запускать эксперимент": "метода проверки и запуска эксперимента",
    "маркетинговые каналы": "маркетинговых каналов",
    "паттерн интеграции llm": "паттерна интеграции LLM",
    "позиционирование": "позиционирования",
    "проблемную гипотезу": "проблемной гипотезы",
    "репозиторий": "Git-репозитория продукта",
    "процесс triage": "процесса triage",
    "продуктовую страницу": "продуктовой страницы",
    "продуктовую стратегию": "продуктовой стратегии",
    "спецификацию": "спецификации",
    "систему тикетов": "системы тикетов",
    "тарифную модель": "тарифной модели",
    "сегмент": "целевого сегмента",
    "целевой сегмент": "целевого сегмента",
    "гипотезу с метриками и критерием успеха": "гипотезы с метриками и критерием успеха",
    "результаты теста": "результатов теста",
    "данные эксперимента и принимать решение": "данных эксперимента и принятие решения",
    "инсайты пользователей": "инсайтов пользователей",
    "архитектуру продукта с включением ai-компонентов": "архитектуры продукта с включением AI-компонентов",
    "ценностное предложение": "ценностного предложения",
    "эксперименты для валидации гипотез": "экспериментов для валидации гипотез",
}

ACTION_NOUNS = {
    "проведение",
    "выбор",
    "определение",
    "формулирование",
    "проектирование",
    "настройка",
    "разработка",
    "подготовка",
    "оценка",
    "анализ",
    "синтез",
    "интерпретация",
    "описание",
    "оформление",
    "составление",
    "систематизация",
    "расчёт",
    "расчет",
    "организация",
    "внедрение",
    "развёртывание",
    "развертывание",
    "приоритизация",
    "создание",
    "ведение",
    "сборка",
    "сбор",
    "запуск",
    "сегментация",
    "картирование",
    "интеграция",
    "применение",
    "использование",
    "обеспечение",
}

_FRAGMENT_REPAIRS = {
    "пробный доступ": "Проектирование механики пробного доступа",
    "пробных доступов": "Проектирование пробных доступов",
    "лендинг продукта": "Создание лендинга продукта для проверки спроса",
    "ключевое сообщение": "Формулирование ключевого сообщения продукта",
    "ключевого сообщения": "Формулирование ключевого сообщения продукта",
    "ценностное предложение": "Формулирование ценностного предложения",
    "релизного процесса": "Организация релизного процесса",
    "автоматических тестов": "Разработка автоматических тестов",
    "каналов привлечения": "Выбор каналов привлечения",
    "монетизационной модели": "Проектирование монетизационной модели",
    "готовности к инцидентам": "Обеспечение готовности к инцидентам",
    "финансовые документы для запуска": "Подготовка базовых финансовых документов запуска",
    "подготовка базовые правовые": "Подготовка базового правового контура запуска",
    "настройка репозиторий": "Настройка Git-репозитория продукта",
    "формулирование сегмент": "Формулирование целевого сегмента",
    "стратегию тестирования": "Проектирование стратегии тестирования",
    "сценариев использования": "Описание сценариев использования",
    "базовой тестовой практики": "Применение базовой тестовой практики",
    "инцидентного реагирования": "Организация инцидентного реагирования",
}

_ACTION_PREFIXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^провести\s+(.+)$", re.IGNORECASE), "Проведение"),
    (re.compile(r"^проводить\s+(.+)$", re.IGNORECASE), "Проведение"),
    (re.compile(r"^сформулировать\s+(.+)$", re.IGNORECASE), "Формулирование"),
    (re.compile(r"^формулировать\s+(.+)$", re.IGNORECASE), "Формулирование"),
    (re.compile(r"^определить\s+(.+)$", re.IGNORECASE), "Определение"),
    (re.compile(r"^определять\s+(.+)$", re.IGNORECASE), "Определение"),
    (re.compile(r"^выбрать\s+(.+)$", re.IGNORECASE), "Выбор"),
    (re.compile(r"^выбирать\s+(.+)$", re.IGNORECASE), "Выбор"),
    (re.compile(r"^подготовить\s+(.+)$", re.IGNORECASE), "Подготовка"),
    (re.compile(r"^готовить\s+(.+)$", re.IGNORECASE), "Подготовка"),
    (re.compile(r"^настроить\s+(.+)$", re.IGNORECASE), "Настройка"),
    (re.compile(r"^настраивать\s+(.+)$", re.IGNORECASE), "Настройка"),
    (re.compile(r"^спроектировать\s+(.+)$", re.IGNORECASE), "Проектирование"),
    (re.compile(r"^проектировать\s+(.+)$", re.IGNORECASE), "Проектирование"),
    (re.compile(r"^разработать\s+(.+)$", re.IGNORECASE), "Разработка"),
    (re.compile(r"^разрабатывать\s+(.+)$", re.IGNORECASE), "Разработка"),
    (re.compile(r"^описать\s+(.+)$", re.IGNORECASE), "Описание"),
    (re.compile(r"^описывать\s+(.+)$", re.IGNORECASE), "Описание"),
    (re.compile(r"^оформить\s+(.+)$", re.IGNORECASE), "Оформление"),
    (re.compile(r"^оформлять\s+(.+)$", re.IGNORECASE), "Оформление"),
    (re.compile(r"^оценить\s+(.+)$", re.IGNORECASE), "Оценка"),
    (re.compile(r"^оценивать\s+(.+)$", re.IGNORECASE), "Оценка"),
    (re.compile(r"^анализировать\s+(.+)$", re.IGNORECASE), "Анализ"),
    (re.compile(r"^синтезировать\s+(.+)$", re.IGNORECASE), "Синтез"),
    (re.compile(r"^собирать\s+(.+)$", re.IGNORECASE), "Сбор"),
    (re.compile(r"^запускать\s+(.+)$", re.IGNORECASE), "Запуск"),
    (re.compile(r"^применять\s+(.+)$", re.IGNORECASE), "Применение"),
    (re.compile(r"^использовать\s+(.+)$", re.IGNORECASE), "Использование"),
    (re.compile(r"^управлять\s+(.+)$", re.IGNORECASE), "Управление"),
    (re.compile(r"^приоритизировать\s+(.+)$", re.IGNORECASE), "Приоритизация"),
    (re.compile(r"^обеспечивать\s+(.+)$", re.IGNORECASE), "Обеспечение"),
]

_FRAGMENT_PREFIXES = {
    "сегментацию": "Сегментация",
    "анализ": "Анализ",
    "дизайн": "Проектирование",
    "расчёт": "Расчёт",
    "расчет": "Расчёт",
}

_GENITIVE_FRAGMENT_HEADS = {
    "автоматических",
    "готовности",
    "каналов",
    "ключевого",
    "монетизационной",
    "пробных",
    "релизного",
    "сценариев",
}

_GENITIVE_FRAGMENT_SUFFIXES = (
    "ого",
    "его",
    "ой",
    "ых",
    "их",
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("‑", "-").strip(" \t\r\n.,;:"))


def _rewrite_object(value: str) -> str:
    cleaned = _clean(value)
    key = cleaned.casefold().replace("ё", "е")
    return _OBJECT_REWRITES.get(key, cleaned)


def looks_like_genitive_fragment(name: str) -> bool:
    """Detect object fragments like "Релизного процесса" without an observable action."""
    cleaned = _clean(name)
    if not cleaned:
        return False
    first = cleaned.split()[0].casefold().replace("ё", "е")
    if first in ACTION_NOUNS:
        return False
    return first in _GENITIVE_FRAGMENT_HEADS or first.endswith(_GENITIVE_FRAGMENT_SUFFIXES)


def canonicalize_skill_name(name: str) -> str:
    """Возвращает нейтральное имя skill без повелительной/action-формы."""
    cleaned = _clean(name)
    if not cleaned:
        return cleaned
    key = cleaned.casefold().replace("ё", "е")
    if key in _FRAGMENT_REPAIRS:
        return _FRAGMENT_REPAIRS[key]

    for pattern, noun in _ACTION_PREFIXES:
        match = pattern.match(cleaned)
        if match:
            return _clean(f"{noun} {_rewrite_object(match.group(1))}")

    first, *rest = cleaned.split()
    if first.casefold().replace("ё", "е") in ACTION_NOUNS and rest:
        return _clean(f"{first[0].upper() + first[1:]} {_rewrite_object(' '.join(rest))}")

    fragment_noun = _FRAGMENT_PREFIXES.get(first.casefold().replace("ё", "е"))
    if fragment_noun:
        return _clean(" ".join([fragment_noun, *rest]))

    return cleaned[0].upper() + cleaned[1:]


def has_observable_action(name: str) -> bool:
    cleaned = _clean(name)
    if not cleaned:
        return False
    first = cleaned.split()[0].casefold().replace("ё", "е")
    return first in ACTION_NOUNS


def skill_name_variants(name: str | None) -> list[str]:
    """Даёт варианты имени для resolve: исходное + канонизированное без дублей."""
    variants: list[str] = []
    for candidate in [name or "", canonicalize_skill_name(name or "")]:
        cleaned = _clean(candidate)
        if cleaned and cleaned.casefold() not in {item.casefold() for item in variants}:
            variants.append(cleaned)
    return variants
