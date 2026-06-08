"""Слой доступа к LLM через OpenRouter (mock/live). Аналог content_gen/llm."""
from __future__ import annotations
import json
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from . import config
from .prompt_versions import prompt_version_for_stage

_USAGE_CONTEXT: ContextVar[dict[str, object]] = ContextVar("llm_usage_context", default={})


def set_usage_context(**kwargs: object) -> None:
    current = dict(_USAGE_CONTEXT.get() or {})
    for key, value in kwargs.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    _USAGE_CONTEXT.set(current)


def clear_usage_context() -> None:
    _USAGE_CONTEXT.set({})


def _append_usage_log(
    model: str,
    messages: list[dict],
    json_mode: bool,
    timeout: int,
    max_tokens: int | None,
    resp: dict,
    latency_ms: float,
) -> None:
    usage = resp.get("usage") or {}
    context = _USAGE_CONTEXT.get() or {}
    stage = context.get("stage")
    record = {
        "logged_at": datetime.now(UTC).isoformat(),
        "job_id": context.get("job_id"),
        "brief_id": context.get("brief_id"),
        "stage": stage,
        "prompt_version": context.get("prompt_version") or prompt_version_for_stage(str(stage) if stage else None),
        "model": model,
        "json_mode": json_mode,
        "timeout_seconds": timeout or config.REQUEST_TIMEOUT_SECONDS,
        "max_tokens": max_tokens,
        "message_count": len(messages),
        "latency_ms": round(latency_ms, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    log_path = Path(config.LLM_USAGE_LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def chat(model: str, messages: list[dict], json_mode: bool = False, timeout: int = 90, max_tokens: int | None = None) -> dict:
    import requests
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY не найден. Проверьте .env в корне проекта.")
    payload: dict = {"model": model, "messages": messages}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    session = requests.Session()
    session.trust_env = False
    started_at = time.perf_counter()
    r = session.post(
        config.OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": config.OPENROUTER_HTTP_REFERER,
            "X-Title": config.OPENROUTER_APP_TITLE,
        },
        json=payload,
        timeout=timeout or config.REQUEST_TIMEOUT_SECONDS,
    )
    latency_ms = (time.perf_counter() - started_at) * 1000
    r.raise_for_status()
    response_json = r.json()
    _append_usage_log(model, messages, json_mode, timeout, max_tokens, response_json, latency_ms)
    return response_json


def content(resp: dict) -> str:
    return resp["choices"][0]["message"]["content"]


def citations(resp: dict) -> list[str]:
    cits = resp.get("citations") or []
    if not cits:
        ann = resp["choices"][0]["message"].get("annotations") or []
        cits = [a.get("url_citation", {}).get("url", "") for a in ann]
    return [c for c in cits if c]
