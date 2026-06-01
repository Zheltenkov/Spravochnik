"""Слой доступа к LLM через OpenRouter (mock/live). Аналог content_gen/llm."""
from __future__ import annotations
import json
from . import config


def chat(model: str, messages: list[dict], json_mode: bool = False, timeout: int = 90) -> dict:
    import requests
    payload: dict = {"model": model, "messages": messages}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(
        config.OPENROUTER_URL,
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def content(resp: dict) -> str:
    return resp["choices"][0]["message"]["content"]


def citations(resp: dict) -> list[str]:
    cits = resp.get("citations") or []
    if not cits:
        ann = resp["choices"][0]["message"].get("annotations") or []
        cits = [a.get("url_citation", {}).get("url", "") for a in ann]
    return [c for c in cits if c]
