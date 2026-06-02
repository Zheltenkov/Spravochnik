"""Слой доступа к LLM через OpenRouter (mock/live). Аналог content_gen/llm."""
from __future__ import annotations
from . import config


def chat(model: str, messages: list[dict], json_mode: bool = False, timeout: int = 90) -> dict:
    import requests
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY не найден. Проверьте .env в корне проекта.")
    payload: dict = {"model": model, "messages": messages}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    session = requests.Session()
    session.trust_env = False
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
