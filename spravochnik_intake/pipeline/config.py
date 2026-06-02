"""Конфиг-как-данные: флаги, пороги, slug'и моделей. Аналог content_gen/config."""
from __future__ import annotations
import os
from pathlib import Path


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_structural_rules(name: str) -> list[tuple[str, str]]:
    raw = os.environ.get(name, "реляцион>SQL;SQL>REST;REST>очеред")
    rules: list[tuple[str, str]] = []
    for chunk in raw.split(";"):
        item = chunk.strip()
        if not item or ">" not in item:
            continue
        left, right = item.split(">", 1)
        src = left.strip()
        dst = right.strip()
        if src and dst:
            rules.append((src, dst))
    return rules


_load_dotenv()

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPEN_ROUTER_API_KEY", "")
USE_LIVE = _env_bool("USE_LIVE", bool(OPENROUTER_API_KEY))
USE_COUNCIL = _env_bool("USE_COUNCIL", True)

OPENROUTER_URL = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_HTTP_REFERER = os.environ.get("OPENROUTER_HTTP_REFERER", "http://127.0.0.1:8010")
OPENROUTER_APP_TITLE = os.environ.get("OPENROUTER_APP_TITLE", "Spravochnik Intake")
MODEL_PLAN = os.environ.get("MODEL_PLAN", "openai/gpt-4.1-mini")
MODEL_SEARCH = os.environ.get("MODEL_SEARCH", "perplexity/sonar-pro")
MODEL_PANEL = [
    item.strip()
    for item in os.environ.get(
        "MODEL_PANEL",
        "openai/gpt-4.1-mini,anthropic/claude-3.5-haiku,google/gemini-2.0-flash-001",
    ).split(",")
    if item.strip()
]

# Пороги триажа (стадия 1->2)
TAU_CONFIDENCE = float(os.environ.get("TAU_CONFIDENCE", "0.75"))
MIN_SOURCES = int(os.environ.get("MIN_SOURCES", "2"))
COUNCIL_AGREE_OK = float(os.environ.get("COUNCIL_AGREE_OK", "0.67"))
FUZZY_MATCH_MIN = int(os.environ.get("FUZZY_MATCH_MIN", "90"))      # rapidfuzz score для fuzzy-резолва

# Стадия 2->3
TAU_EDGE_ACCEPT = float(os.environ.get("TAU_EDGE_ACCEPT", "0.80"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "90"))
STRUCTURAL_PREREQ_RULES = _env_structural_rules("STRUCTURAL_PREREQ_RULES")
