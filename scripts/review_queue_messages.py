from __future__ import annotations

import re


REVIEW_REASON_LABELS = {
    "missing_dimension": "Не указан тип индикатора",
    "missing_block_title": "У блока нет названия",
    "orphan_indicator_row": "Строка не привязана к skill",
    "ambiguous_skill_name": "Нужно уточнить название skill",
    "no_header_rows": "Не найден заголовок блока",
    "ambiguous_block_title": "Нужно уточнить название блока",
    "level_headers_inherited_from_previous_block": "Шкала унаследована от предыдущего блока",
    "skill_name_trimmed": "Название skill было очищено от лишних пробелов",
    "base_text_without_levels": "Есть текст индикатора без уровней",
}


def parse_source_ref(source_ref: str | None) -> tuple[str | None, str | None, int | None]:
    if not source_ref:
        return None, None, None
    parts = [part.strip() for part in str(source_ref).split("::")]
    workbook = parts[0] if parts else None
    sheet = parts[1] if len(parts) > 1 else None
    row_no = None
    if len(parts) > 2:
        match = re.search(r"row-(\d+)", parts[2], flags=re.IGNORECASE)
        if match:
            row_no = int(match.group(1))
    return workbook, sheet, row_no


def location_phrase(source_ref: str | None) -> str:
    workbook, sheet, row_no = parse_source_ref(source_ref)
    bits: list[str] = []
    if workbook:
        bits.append(f"файл «{workbook}»")
    if sheet:
        bits.append(f"лист «{sheet}»")
    if row_no is not None:
        bits.append(f"строка {row_no}")
    if not bits:
        return "Источник не указан."
    return "Источник: " + ", ".join(bits) + "."


def extract_subject(details: str | None) -> str | None:
    if not details:
        return None
    for pattern in (r"'([^']+)'", r"«([^»]+)»", r":\s*(.+?)(?:\.\s*$|$)"):
        match = re.search(pattern, details)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def reason_label(reason_code: str | None) -> str:
    if not reason_code:
        return "Нужна проверка"
    return REVIEW_REASON_LABELS.get(reason_code, reason_code.replace("_", " "))


def humanize_review_details(reason_code: str, source_ref: str | None, original_details: str | None) -> str:
    location = location_phrase(source_ref)
    subject = extract_subject(original_details)

    if reason_code == "missing_dimension":
        return (
            f"{location} У индикатора не указан тип: «Знает», «Умеет» или «Владеет». "
            "Нужно выбрать один подходящий тип вручную, чтобы строка попала в правильную часть skill."
        )

    if reason_code == "missing_block_title":
        return (
            f"{location} У блока нет названия. "
            "Нужно добавить понятное название группы skills или перенести строки в соседний корректный блок."
        )

    if reason_code == "orphan_indicator_row":
        return (
            f"{location} В строке есть индикатор, но система не смогла определить, к какому skill он относится. "
            "Нужно вручную привязать строку к нужному skill или удалить лишний фрагмент."
        )

    if reason_code == "ambiguous_skill_name":
        if subject:
            return (
                f"{location} Название skill сформулировано неоднозначно: «{subject}». "
                "Нужно оставить одно короткое и понятное название без вопросительных знаков и комментариев."
            )
        return (
            f"{location} Название skill сформулировано неоднозначно. "
            "Нужно переписать его коротко и однозначно."
        )

    if reason_code == "no_header_rows":
        return (
            f"{location} На листе не найден стандартный заголовок блока skills, поэтому лист не удалось разобрать автоматически. "
            "Нужно проверить, это рабочий лист каталога, служебный лист или лист с нестандартной структурой."
        )

    if reason_code == "ambiguous_block_title":
        if subject:
            return (
                f"{location} Название блока выглядит спорным: «{subject}». "
                "Нужно подтвердить, что это отдельная группа skills, или заменить название на более точное."
            )
        return (
            f"{location} Название блока выглядит спорным. "
            "Нужно уточнить формулировку и проверить, нужен ли этот блок в каталоге."
        )

    if reason_code == "level_headers_inherited_from_previous_block":
        if subject:
            return (
                f"{location} У блока «{subject}» не нашлась собственная строка с уровнями, "
                "поэтому система временно взяла шкалу из предыдущего блока. Нужно подтвердить шкалу или прописать её заново."
            )
        return (
            f"{location} У блока не нашлась собственная строка с уровнями, "
            "поэтому система временно взяла шкалу из предыдущего блока. Нужно подтвердить шкалу или прописать её заново."
        )

    if reason_code == "skill_name_trimmed":
        if subject:
            return (
                f"{location} Название skill «{subject}» было автоматически очищено от лишних пробелов. "
                "Стоит проверить, что после очистки формулировка осталась корректной."
            )
        return (
            f"{location} Название skill было автоматически очищено от лишних пробелов. "
            "Стоит проверить, что формулировка осталась корректной."
        )

    if reason_code == "base_text_without_levels":
        return (
            f"{location} У строки есть текст индикатора, но не заполнены уровни или значения по шкале. "
            "Нужно решить, оставить общий текст без уровней или заполнить шкалу полностью."
        )

    if original_details:
        return f"{location} {original_details.strip()}"
    return f"{location} Нужна ручная проверка записи."
