"""Стадия нормализации и безопасной дедупликации атомарных skill-кандидатов.

Задача этой стадии:
1. Привести очевидно эквивалентные названия к единому виду.
2. Слить exact/near-exact дубли до резолва против каталога.
3. Не трогать разные по смыслу skills, даже если они находятся в одной coverage-area.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

from . import config
from .models import IndicatorSpec, SkillCandidate
from .skill_names import looks_like_genitive_fragment


_ACTION_NORMALIZATION = {
    "формализация": "формулирование",
    "интеграция": "настройка",
    "setup": "настройка",
}

_DROP_TOKENS = {
    "для",
    "задач",
    "user",
    "story",
    "workflow",
    "process",
    "простого",
    "базового",
}


@dataclass(slots=True)
class MergeDecision:
    score: float
    reason: str


def _normalize_name(value: str) -> str:
    # Нормализуем Unicode и базовую пунктуацию, чтобы одинаковые формулировки сравнивались стабильно.
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    text = text.replace("‑", "-").replace("–", "-").replace("—", "-")
    text = text.replace("ci cd", "ci/cd").replace("ci / cd", "ci/cd")
    text = text.replace("m v p", "mvp").replace("ok r", "okr")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^0-9a-zа-я+\-/ ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_token(token: str) -> str:
    # Простая нормализация токенов нужна для дедупа, не для публикации канонического имени.
    token = _ACTION_NORMALIZATION.get(token, token)
    if token in _DROP_TOKENS:
        return ""
    # Грубый стемминг снижает чувствительность к падежам и вариациям типа "платежный/платежного".
    for suffix in (
        "иями",
        "ями",
        "ами",
        "ого",
        "его",
        "ому",
        "ему",
        "ыми",
        "ими",
        "ыми",
        "ыми",
        "ий",
        "ый",
        "ой",
        "ая",
        "ое",
        "ые",
        "ые",
        "ов",
        "ев",
        "ам",
        "ям",
        "ах",
        "ях",
        "ия",
        "ие",
        "ий",
        "иям",
        "ию",
        "ии",
        "ть",
        "ти",
        "ция",
        "ции",
        "цией",
        "цией",
    ):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            token = token[: -len(suffix)]
            break
    return token


def _token_signature(value: str) -> list[str]:
    tokens = []
    for token in _normalize_name(value).split():
        normalized = _normalize_token(token)
        if normalized:
            tokens.append(normalized)
    return tokens


def _jaccard(left: list[str], right: list[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _sequence_ratio(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def _similarity(left: SkillCandidate, right: SkillCandidate) -> MergeDecision | None:
    # Дедуп допускается только внутри одной coverage-area или очень близкой группы.
    left_area = (left.coverage_area or "").strip()
    right_area = (right.coverage_area or "").strip()
    if left_area and right_area and left_area != right_area:
        return None

    left_group = (left.group or "").strip().casefold()
    right_group = (right.group or "").strip().casefold()
    if left_area == "" and right_area == "" and left_group and right_group and left_group != right_group:
        return None

    left_norm = _normalize_name(left.name)
    right_norm = _normalize_name(right.name)
    if left_norm == right_norm:
        return MergeDecision(1.0, "exact_normalized_name")

    left_tokens = _token_signature(left.name)
    right_tokens = _token_signature(right.name)
    if not left_tokens or not right_tokens:
        return None

    jaccard = _jaccard(left_tokens, right_tokens)
    ratio = _sequence_ratio(" ".join(left_tokens), " ".join(right_tokens))

    # Подмножество токенов для случаев типа "Integration of payment provider" vs "payment gateway integration".
    subset_overlap = (
        len(set(left_tokens) & set(right_tokens)) >= 2
        and (set(left_tokens).issubset(set(right_tokens)) or set(right_tokens).issubset(set(left_tokens)))
    )

    if jaccard >= 0.86 or ratio >= 0.93:
        return MergeDecision(max(jaccard, ratio), "near_exact_duplicate")
    if subset_overlap and max(jaccard, ratio) >= 0.72:
        return MergeDecision(max(jaccard, ratio), "subset_duplicate")
    return None


def _dedup_indicators(indicators: list[IndicatorSpec]) -> list[IndicatorSpec]:
    # Индикаторы объединяем без потери уникальных формулировок.
    seen: set[tuple[str, str]] = set()
    out: list[IndicatorSpec] = []
    for indicator in indicators:
        key = (_normalize_name(indicator.text), indicator.bloom)
        if key in seen:
            continue
        seen.add(key)
        out.append(indicator)
    return out


def _merge_into(anchor: SkillCandidate, duplicate: SkillCandidate) -> None:
    # В survivor переносим provenance, evidence и индикаторы из дубля.
    anchor.evidence_ids = list(dict.fromkeys([*anchor.evidence_ids, *duplicate.evidence_ids]))
    anchor.tools = list(dict.fromkeys([*anchor.tools, *duplicate.tools]))
    anchor.indicators = _dedup_indicators([*anchor.indicators, *duplicate.indicators])
    if not anchor.coverage_area and duplicate.coverage_area:
        anchor.coverage_area = duplicate.coverage_area
    anchor.confidence = max(anchor.confidence, duplicate.confidence)
    if duplicate.council_agreement is not None:
        anchor.council_agreement = max(anchor.council_agreement or 0.0, duplicate.council_agreement)


def _candidate_rank(candidate: SkillCandidate) -> tuple[int, int, int, int]:
    # Более богатое evidence и более компактное название обычно дают лучший представитель для coverage-area.
    evidence_score = len(set(candidate.evidence_ids))
    indicator_score = len(candidate.indicators)
    token_count = len(_token_signature(candidate.name))
    compact_name_bonus = -abs(token_count - 4)
    tool_score = len(candidate.tools)
    quality_score = 0 if _looks_fragmentary(candidate.name) else 1
    return (quality_score, evidence_score, indicator_score, tool_score, compact_name_bonus)


def _looks_fragmentary(name: str) -> bool:
    stripped = name.strip()
    if not stripped:
        return True
    if looks_like_genitive_fragment(stripped):
        return True
    # Обрубки после неудачного split обычно начинаются со строчной буквы и не выглядят как законченный action-skill.
    if stripped[0].islower():
        return True
    if len(_token_signature(name)) < 2:
        return True
    return False


def _must_drop_fragment(name: str) -> bool:
    """Hard filter only fragments that cannot be interpreted as an observable skill."""
    return looks_like_genitive_fragment(name)


def _apply_area_compaction(
    candidates: list[SkillCandidate],
    *,
    max_per_area: int,
) -> tuple[list[SkillCandidate], list[dict[str, object]]]:
    if max_per_area <= 0:
        return candidates, []

    grouped: dict[str, list[SkillCandidate]] = {}
    passthrough: list[SkillCandidate] = []
    compacted_events: list[dict[str, object]] = []
    for candidate in candidates:
        if candidate.entity_type == "skill" and candidate.atomicity == "atomic" and _must_drop_fragment(candidate.name):
            compacted_events.append(
                {
                    "coverage_area": candidate.coverage_area,
                    "kept_names": [],
                    "dropped_names": [candidate.name],
                    "reason": "program_brief_genitive_fragment_filter",
                }
            )
            continue
        if not (candidate.entity_type == "skill" and candidate.atomicity == "atomic" and candidate.coverage_area):
            passthrough.append(candidate)
            continue
        grouped.setdefault(candidate.coverage_area, []).append(candidate)

    kept: list[SkillCandidate] = list(passthrough)
    for area, area_candidates in grouped.items():
        has_non_fragment = any(not _looks_fragmentary(candidate.name) for candidate in area_candidates)
        if len(area_candidates) <= max_per_area:
            for candidate in area_candidates:
                if has_non_fragment and _looks_fragmentary(candidate.name):
                    compacted_events.append(
                        {
                            "coverage_area": area,
                            "kept_names": [item.name for item in area_candidates if not _looks_fragmentary(item.name)],
                            "dropped_names": [candidate.name],
                            "reason": "program_brief_fragment_filter",
                        }
                    )
                    continue
                kept.append(candidate)
            continue

        ranked = sorted(
            area_candidates,
            key=lambda candidate: (_candidate_rank(candidate), -len(candidate.name)),
            reverse=True,
        )
        area_kept: list[SkillCandidate] = []
        area_dropped: list[SkillCandidate] = []
        for candidate in ranked:
            if len(area_kept) < max_per_area:
                if area_kept and _looks_fragmentary(candidate.name):
                    area_dropped.append(candidate)
                    continue
                area_kept.append(candidate)
                continue
            area_dropped.append(candidate)
        kept.extend(area_kept)
        compacted_events.append(
            {
                "coverage_area": area,
                "kept_names": [candidate.name for candidate in area_kept],
                "dropped_names": [candidate.name for candidate in area_dropped],
                "reason": f"program_brief_area_cap:{max_per_area}",
            }
        )
    return kept, compacted_events


def run(candidates: list[SkillCandidate], spec: dict[str, object] | None = None) -> tuple[list[SkillCandidate], dict[str, object]]:
    """Возвращает сокращённый список кандидатов и детальный audit merge-событий."""
    kept: list[SkillCandidate] = []
    atomic_input = 0
    atomic_output = 0
    exact_merges = 0
    fuzzy_merges = 0
    dedup_events: list[dict[str, object]] = []

    for candidate in candidates:
        if not (candidate.entity_type == "skill" and candidate.atomicity == "atomic"):
            kept.append(candidate)
            continue

        atomic_input += 1
        duplicate_target: SkillCandidate | None = None
        duplicate_decision: MergeDecision | None = None
        for anchor in kept:
            if not (anchor.entity_type == "skill" and anchor.atomicity == "atomic"):
                continue
            decision = _similarity(anchor, candidate)
            if decision is None:
                continue
            duplicate_target = anchor
            duplicate_decision = decision
            break

        if duplicate_target is None or duplicate_decision is None:
            kept.append(candidate)
            atomic_output += 1
            continue

        _merge_into(duplicate_target, candidate)
        if duplicate_decision.reason == "exact_normalized_name":
            exact_merges += 1
        else:
            fuzzy_merges += 1
        dedup_events.append(
            {
                "kept_name": duplicate_target.name,
                "absorbed_name": candidate.name,
                "reason": duplicate_decision.reason,
                "score": round(duplicate_decision.score, 2),
                "coverage_area": duplicate_target.coverage_area or candidate.coverage_area,
            }
        )

    compacted_events: list[dict[str, object]] = []
    artifact_type = str((spec or {}).get("artifact_type") or "").strip()
    if artifact_type in {"program_brief", "mixed"}:
        kept, compacted_events = _apply_area_compaction(
            kept,
            max_per_area=config.PROGRAM_BRIEF_MAX_SKILLS_PER_AREA,
        )

    final_atomic_output = len([candidate for candidate in kept if candidate.entity_type == "skill" and candidate.atomicity == "atomic"])
    return kept, {
        "atomic_input_count": atomic_input,
        "atomic_output_count": final_atomic_output,
        "merged_count": exact_merges + fuzzy_merges,
        "exact_merged_count": exact_merges,
        "fuzzy_merged_count": fuzzy_merges,
        "compacted_count": sum(len(item["dropped_names"]) for item in compacted_events),
        "events": dedup_events,
        "compaction_events": compacted_events,
    }
