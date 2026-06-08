from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spravochnik_intake.pipeline import config as intake_config
from spravochnik_intake.pipeline.prompt_versions import prompt_version_for_stage

STAGE_LABELS: dict[str, str] = {
    "decompose": "Декомпозиция",
    "draft": "Черновик навыков",
    "atomize": "Атомизация",
    "normalize": "Нормализация",
    "resolve": "Сопоставление с каталогом",
    "search": "Поиск подтверждений",
    "council": "Экспертное жюри",
    "triage": "Триаж",
    "dag": "Граф зависимостей",
    "up_template_consilium": "Шаблоны УП",
}


def _reason_set(reasons: list[str] | tuple[str, ...] | str | None) -> set[str]:
    if not reasons:
        return set()
    if isinstance(reasons, str):
        raw_items = reasons.split(",")
    else:
        raw_items = list(reasons)
    normalized: set[str] = set()
    for item in raw_items:
        value = str(item or "").strip()
        if value:
            normalized.add(value)
    return normalized


def _safe_percent(part: int | float, total: int | float) -> float:
    return round((float(part) / float(total) * 100.0), 1) if total else 0.0


@dataclass(frozen=True)
class DecisionRationale:
    summary: str
    match_evidence: str
    council_rationale: str
    validator_reasons: str

    def as_template_dict(self) -> dict[str, str]:
        return {
            "summary": self.summary,
            "match_evidence": self.match_evidence,
            "council_rationale": self.council_rationale,
            "validator_reasons": self.validator_reasons,
        }


def build_model_version_rows(llm_usage: dict[str, Any] | None) -> list[dict[str, object]]:
    rows = []
    for row in (llm_usage or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        stage = str(row.get("stage") or "unknown")
        rows.append(
            {
                "stage": stage,
                "stage_label": STAGE_LABELS.get(stage, stage),
                "model": str(row.get("model") or "unknown"),
                "prompt_version": str(row.get("prompt_version") or prompt_version_for_stage(stage)),
                "calls": int(row.get("calls") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
                "total_latency_ms": int(row.get("total_latency_ms") or 0),
                "avg_latency_ms": int(row.get("avg_latency_ms") or 0),
                "estimated_cost_label": str(row.get("estimated_cost_label") or "—"),
            }
        )
    return rows


def load_llm_usage_summary(job_id: int) -> dict[str, object]:
    path = Path(intake_config.LLM_USAGE_LOG_PATH)
    if not path.exists():
        return {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_latency_ms": 0,
            "avg_latency_ms": 0,
            "estimated_cost_usd": None,
            "estimated_cost_label": "—",
            "cost_configured": False,
            "rows": [],
        }
    aggregate: dict[tuple[str, str, str], dict[str, object]] = {}
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    total_latency_ms = 0.0
    total_calls = 0
    total_cost: float | None = 0.0 if intake_config.LLM_PRICE_USD_PER_1M else None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(record.get("job_id") or 0) != job_id:
                continue
            stage = str(record.get("stage") or "unknown")
            model = str(record.get("model") or "unknown")
            prompt_version = str(record.get("prompt_version") or "")
            key = (stage, model, prompt_version)
            row = aggregate.setdefault(
                key,
                {
                    "stage": stage,
                    "model": model,
                    "prompt_version": prompt_version,
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "total_latency_ms": 0.0,
                    "avg_latency_ms": 0,
                    "estimated_cost_usd": None,
                    "estimated_cost_label": "—",
                },
            )
            row["calls"] = int(row["calls"]) + 1
            total_calls += 1
            latency_ms = float(record.get("latency_ms") or 0.0)
            row["total_latency_ms"] = float(row["total_latency_ms"]) + latency_ms
            total_latency_ms += latency_ms
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = int(record.get(field) or 0)
                row[field] = int(row[field]) + value
                totals[field] += value
            prices = intake_config.LLM_PRICE_USD_PER_1M.get(model)
            if prices and total_cost is not None:
                prompt_cost = int(record.get("prompt_tokens") or 0) * prices[0] / 1_000_000
                completion_cost = int(record.get("completion_tokens") or 0) * prices[1] / 1_000_000
                row_cost = float(row["estimated_cost_usd"] or 0.0) + prompt_cost + completion_cost
                row["estimated_cost_usd"] = row_cost
                row["estimated_cost_label"] = f"${row_cost:.4f}"
                total_cost += prompt_cost + completion_cost
    rows = sorted(aggregate.values(), key=lambda item: (str(item["stage"]), str(item["model"])))
    for row in rows:
        calls = max(int(row.get("calls") or 0), 1)
        row["avg_latency_ms"] = int(round(float(row.get("total_latency_ms") or 0.0) / calls))
        row["total_latency_ms"] = int(round(float(row.get("total_latency_ms") or 0.0)))
        if row.get("estimated_cost_usd") is None:
            row["estimated_cost_label"] = "—"
    return {
        **totals,
        "total_latency_ms": int(round(total_latency_ms)),
        "avg_latency_ms": int(round(total_latency_ms / total_calls)) if total_calls else 0,
        "estimated_cost_usd": total_cost,
        "estimated_cost_label": f"${total_cost:.4f}" if total_cost is not None else "—",
        "cost_configured": total_cost is not None,
        "rows": rows,
    }


def build_intake_quality_metrics(
    result: dict[str, object] | None,
    llm_usage: dict[str, object] | None,
) -> dict[str, object] | None:
    """Compact quality dashboard for a completed intake job."""
    if not isinstance(result, dict):
        return None
    candidates = [item for item in result.get("candidates") or [] if isinstance(item, dict)]
    atomic = [item for item in candidates if item.get("entity_type") == "skill" and item.get("atomicity") == "atomic"]
    decisions = {
        "accepted": len([item for item in atomic if item.get("decision") == "accepted"]),
        "review": len([item for item in atomic if item.get("decision") == "needs_review"]),
        "rejected": len([item for item in atomic if item.get("decision") == "rejected"]),
    }
    catalog_matches = [
        item
        for item in atomic
        if str(item.get("resolution") or "").casefold() in {"matched", "alias", "fuzzy"}
    ]
    suspicious_matches = [
        item
        for item in catalog_matches
        if "catalog_match_suspicious" in _reason_set(item.get("reasons"))
        or item.get("decision") == "rejected"
    ]
    false_match_count = len(suspicious_matches)
    up_payload = result.get("curriculum_plan") if isinstance(result.get("curriculum_plan"), dict) else {}
    up_report = up_payload.get("report") if isinstance(up_payload, dict) and isinstance(up_payload.get("report"), dict) else {}
    up_quality = up_report.get("quality_metrics") if isinstance(up_report.get("quality_metrics"), dict) else {}
    up_rows = [row for row in up_payload.get("rows") or [] if isinstance(row, dict)] if isinstance(up_payload, dict) else []
    up_summary = up_payload.get("summary") if isinstance(up_payload.get("summary"), dict) else {}
    dag_payload = result.get("dag") if isinstance(result.get("dag"), dict) else {}
    dag_waves = dag_payload.get("visual_waves") or dag_payload.get("waves") or []
    template_proposal_count = int(up_payload.get("template_proposal_count") or 0) if isinstance(up_payload, dict) else 0
    enriched_rows = [
        row
        for row in up_rows
        if row.get("project_summary")
        and row.get("artifact")
        and row.get("materials")
        and row.get("storytelling")
        and row.get("validation_criteria")
    ]
    llm = llm_usage or {}
    return {
        "candidate_count": len(atomic),
        "accepted_count": decisions["accepted"],
        "review_count": decisions["review"],
        "rejected_count": decisions["rejected"],
        "accepted_rate_pct": _safe_percent(decisions["accepted"], len(atomic)),
        "review_rate_pct": _safe_percent(decisions["review"], len(atomic)),
        "catalog_match_count": len(catalog_matches),
        "false_match_count": false_match_count,
        "false_match_rate_pct": _safe_percent(false_match_count, len(catalog_matches)),
        "llm_total_tokens": int(llm.get("total_tokens") or 0),
        "llm_avg_latency_ms": int(llm.get("avg_latency_ms") or 0),
        "llm_total_latency_ms": int(llm.get("total_latency_ms") or 0),
        "llm_estimated_cost_label": str(llm.get("estimated_cost_label") or "—"),
        "llm_cost_configured": bool(llm.get("cost_configured")),
        "up_project_count": len(up_rows),
        "dag_wave_count": len(dag_waves) if isinstance(dag_waves, list) else 0,
        "up_block_count": int(up_summary.get("blocks") or 0),
        "template_proposal_count": template_proposal_count,
        "single_skill_project_count": int(up_quality.get("single_skill_project_count") or 0),
        "avg_skills_per_project": up_quality.get("avg_skills_per_project", 0),
        "avg_primary_skills_per_project": up_quality.get("avg_primary_skills_per_project", 0),
        "avg_repeat_skills_per_project": up_quality.get("avg_repeat_skills_per_project", 0),
        "avg_outcomes_per_project": up_quality.get("avg_outcomes_per_project", 0),
        "enriched_project_count": len(enriched_rows),
        "enrichment_completeness_pct": _safe_percent(len(enriched_rows), len(up_rows)),
    }


def build_stage_latency_rows(llm_usage: dict[str, Any] | None) -> list[dict[str, object]]:
    aggregate: dict[str, dict[str, object]] = {}
    for row in build_model_version_rows(llm_usage):
        stage = str(row["stage"])
        target = aggregate.setdefault(
            stage,
            {
                "stage": stage,
                "stage_label": row["stage_label"],
                "calls": 0,
                "total_latency_ms": 0,
                "total_tokens": 0,
                "models": set(),
            },
        )
        target["calls"] = int(target["calls"]) + int(row["calls"])
        target["total_latency_ms"] = int(target["total_latency_ms"]) + int(row["total_latency_ms"])
        target["total_tokens"] = int(target["total_tokens"]) + int(row["total_tokens"])
        target["models"].add(str(row["model"]))
    result = []
    for item in aggregate.values():
        calls = max(int(item["calls"]), 1)
        models = sorted(str(model) for model in item["models"])
        result.append(
            {
                "stage": item["stage"],
                "stage_label": item["stage_label"],
                "calls": item["calls"],
                "total_latency_ms": item["total_latency_ms"],
                "avg_latency_ms": int(round(int(item["total_latency_ms"]) / calls)),
                "total_tokens": item["total_tokens"],
                "models": ", ".join(models),
            }
        )
    return sorted(result, key=lambda item: str(item["stage"]))


def build_decision_rationale(candidate: dict[str, Any]) -> dict[str, str]:
    resolution = str(candidate.get("resolution") or "unknown")
    decision = str(candidate.get("decision") or "pending")
    match_score = candidate.get("match_score", "—")
    nearest = str(candidate.get("nearest_name") or "").strip()
    nearest_group = str(candidate.get("nearest_group") or "").strip()
    confidence = str(candidate.get("confidence") or "—")
    council = str(candidate.get("council_agreement") or "—")
    reasons = str(candidate.get("reasons") or "—")

    if nearest:
        match_evidence = f"{resolution}: похожесть {match_score}; ближайший skill: {nearest}"
        if nearest_group:
            match_evidence += f" ({nearest_group})"
    else:
        match_evidence = f"{resolution}: похожесть {match_score}; ближайший skill не найден"

    if council not in {"", "—", "None"}:
        council_rationale = f"Согласие жюри {council}; уверенность модели {confidence}"
    else:
        council_rationale = f"Council не применялся; уверенность модели {confidence}"

    if decision == "accepted":
        summary = "Используется в рабочем контуре: accepted skill может быть применён в справочник, DAG и УП."
    elif decision == "rejected":
        summary = "Не используется для покрытия требований брифа и не попадает в DAG/УП."
    elif nearest:
        summary = "Требует решения методолога: создать новый skill или привязать к ближайшему canonical skill."
    else:
        summary = "Требует решения методолога: похожего canonical skill не найдено."

    return DecisionRationale(
        summary=summary,
        match_evidence=match_evidence,
        council_rationale=council_rationale,
        validator_reasons=reasons,
    ).as_template_dict()


def build_job_observability(llm_usage: dict[str, Any] | None) -> dict[str, object]:
    return {
        "stage_latency_rows": build_stage_latency_rows(llm_usage),
        "model_version_rows": build_model_version_rows(llm_usage),
    }
