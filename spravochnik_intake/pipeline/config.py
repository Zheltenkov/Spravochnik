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


def _env_bloom_ceiling(name: str) -> dict[str, str]:
    raw = os.environ.get(
        name,
        "intro:analyze,junior:analyze,junior+:analyze,начинающий:analyze,базовый:analyze",
    )
    ceilings: dict[str, str] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item or ":" not in item:
            continue
        seniority, bloom = item.split(":", 1)
        seniority_key = seniority.strip().casefold()
        bloom_value = bloom.strip().casefold()
        if seniority_key and bloom_value:
            ceilings[seniority_key] = bloom_value
    return ceilings


def _env_model_price_map(name: str) -> dict[str, tuple[float, float]]:
    """Parse `model:prompt_usd_per_1m:completion_usd_per_1m;...`."""
    raw = os.environ.get(name, "")
    prices: dict[str, tuple[float, float]] = {}
    for chunk in raw.split(";"):
        item = chunk.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split(":")]
        if len(parts) != 3:
            continue
        model, prompt_price, completion_price = parts
        try:
            prices[model] = (float(prompt_price), float(completion_price))
        except ValueError:
            continue
    return prices


_load_dotenv()

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPEN_ROUTER_API_KEY", "")
USE_LIVE = _env_bool("USE_LIVE", bool(OPENROUTER_API_KEY))
USE_COUNCIL = _env_bool("USE_COUNCIL", True)

OPENROUTER_URL = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_HTTP_REFERER = os.environ.get("OPENROUTER_HTTP_REFERER", "http://127.0.0.1:8010")
OPENROUTER_APP_TITLE = os.environ.get("OPENROUTER_APP_TITLE", "Spravochnik Intake")
LLM_USAGE_LOG_PATH = os.environ.get(
    "LLM_USAGE_LOG_PATH",
    str(Path(__file__).resolve().parents[2] / "artifacts" / "llm_usage.jsonl"),
)
LLM_PRICE_USD_PER_1M = _env_model_price_map("LLM_PRICE_USD_PER_1M")
EVIDENCE_CACHE_TTL_DAYS = int(os.environ.get("EVIDENCE_CACHE_TTL_DAYS", "30"))
MODEL_PLAN = os.environ.get("MODEL_PLAN", "openai/gpt-5-mini")
MODEL_SEARCH = os.environ.get("MODEL_SEARCH", "perplexity/sonar")
MODEL_SEARCH_MAX_TOKENS = int(os.environ.get("MODEL_SEARCH_MAX_TOKENS", "1200"))
MODEL_PANEL = [
    item.strip()
    for item in os.environ.get(
        "MODEL_PANEL",
        "openai/gpt-5-mini,anthropic/claude-3.5-haiku,google/gemini-2.0-flash-001",
    ).split(",")
    if item.strip()
]

# Пороги триажа (стадия 1->2)
TAU_CONFIDENCE = float(os.environ.get("TAU_CONFIDENCE", "0.75"))
MIN_SOURCES = int(os.environ.get("MIN_SOURCES", "2"))
COUNCIL_AGREE_OK = float(os.environ.get("COUNCIL_AGREE_OK", "0.67"))
AUTO_ACCEPT_CONFIDENCE = float(os.environ.get("AUTO_ACCEPT_CONFIDENCE", "0.95"))
AUTO_ACCEPT_COUNCIL_AGREEMENT = float(os.environ.get("AUTO_ACCEPT_COUNCIL_AGREEMENT", "1.0"))
AUTO_ACCEPT_NEW_FOR_PROGRAM_BRIEF = _env_bool("AUTO_ACCEPT_NEW_FOR_PROGRAM_BRIEF", False)
PROGRAM_BRIEF_MAX_SKILLS_PER_AREA = int(os.environ.get("PROGRAM_BRIEF_MAX_SKILLS_PER_AREA", "4"))
FUZZY_MATCH_MIN = int(os.environ.get("FUZZY_MATCH_MIN", "90"))      # rapidfuzz score для fuzzy-резолва
GRAY_SEARCH_MAX_QUERIES = int(os.environ.get("GRAY_SEARCH_MAX_QUERIES", "3"))

# Стадия 2->3
TAU_EDGE_ACCEPT = float(os.environ.get("TAU_EDGE_ACCEPT", "0.80"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "90"))
STRUCTURAL_PREREQ_RULES = _env_structural_rules("STRUCTURAL_PREREQ_RULES")

# Стадия 3->4: DAG -> учебный план (верхний планировщик)
USE_UP_TEMPLATE_CONSILIUM = _env_bool("USE_UP_TEMPLATE_CONSILIUM", USE_LIVE)
MODEL_TEMPLATE_COUNCIL = os.environ.get("MODEL_TEMPLATE_COUNCIL", MODEL_PLAN)
MODEL_TEMPLATE_COUNCIL_MAX_TOKENS = int(os.environ.get("MODEL_TEMPLATE_COUNCIL_MAX_TOKENS", "4000"))
UP_TEMPLATE_COUNCIL_TIMEOUT_SECONDS = int(os.environ.get("UP_TEMPLATE_COUNCIL_TIMEOUT_SECONDS", str(REQUEST_TIMEOUT_SECONDS)))
UP_HOURS_PER_DAY = float(os.environ.get("UP_HOURS_PER_DAY", "2.94"))
UP_XP_PER_HOUR = int(os.environ.get("UP_XP_PER_HOUR", "10"))
UP_MAX_SKILLS_PER_PROJECT = int(os.environ.get("UP_MAX_SKILLS_PER_PROJECT", "4"))
UP_TARGET_SKILLS_MIN = int(os.environ.get("UP_TARGET_SKILLS_MIN", "2"))
UP_TARGET_SKILLS_MAX = int(os.environ.get("UP_TARGET_SKILLS_MAX", "4"))
UP_TARGET_OUTCOMES_MIN = int(os.environ.get("UP_TARGET_OUTCOMES_MIN", "3"))
UP_TARGET_OUTCOMES_MAX = int(os.environ.get("UP_TARGET_OUTCOMES_MAX", "5"))
UP_SPIRAL_ENABLED = _env_bool("UP_SPIRAL_ENABLED", True)
UP_CORE_THREAD_MIN = int(os.environ.get("UP_CORE_THREAD_MIN", "4"))
UP_CORE_THREAD_MAX = int(os.environ.get("UP_CORE_THREAD_MAX", "8"))
UP_MIN_THREAD_OCCURRENCES = int(os.environ.get("UP_MIN_THREAD_OCCURRENCES", "2"))
UP_MAX_THREAD_OCCURRENCES = int(os.environ.get("UP_MAX_THREAD_OCCURRENCES", "3"))
UP_SPIRAL_MIN_GAP = int(os.environ.get("UP_SPIRAL_MIN_GAP", "2"))
UP_SPIRAL_GAP_GROWTH = int(os.environ.get("UP_SPIRAL_GAP_GROWTH", "2"))
UP_MAX_PROJECTS_PER_BLOCK = int(os.environ.get("UP_MAX_PROJECTS_PER_BLOCK", "4"))
UP_MAX_THEMES_PER_BLOCK = int(os.environ.get("UP_MAX_THEMES_PER_BLOCK", "2"))
UP_MAX_BLOOM_BY_SENIORITY = _env_bloom_ceiling("UP_MAX_BLOOM_BY_SENIORITY")
UP_GENERATED_COLUMNS = list("ABCDEFGHIJKLMN")
UP_IGNORED_COLUMNS = list("OPQRSTUV")
UP_BLOOM_KNOW = {"remember", "understand"}
UP_BLOOM_CAN = {"apply", "analyze"}
UP_BLOOM_SKILLS = {"evaluate", "create"}
UP_DEFAULT_FORMAT = os.environ.get("UP_DEFAULT_FORMAT", "индивидуальный")
UP_FORMAT_GROUP_SIZES = {
    "индивидуальный": (1, 1),
    "парный": (2, 2),
    "мини-группа": (3, 5),
    "групповой": (3, 5),
}
UP_HOUR_BANDS = [
    int(item.strip())
    for item in os.environ.get("UP_HOUR_BANDS", "8,12,16,20,24").split(",")
    if item.strip()
]
