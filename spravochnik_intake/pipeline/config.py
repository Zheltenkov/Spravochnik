"""Конфиг-как-данные: флаги, пороги, slug'и моделей. Аналог content_gen/config."""
from __future__ import annotations
import os

USE_LIVE = os.environ.get("USE_LIVE", "0") == "1"   # реальные вызовы через OpenRouter
USE_COUNCIL = True

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_PLAN = "openai/gpt-4o-mini"
MODEL_SEARCH = "perplexity/sonar-pro"
MODEL_PANEL = ["openai/gpt-4o-mini", "anthropic/claude-3.5-haiku", "google/gemini-flash-1.5"]

# Пороги триажа (стадия 1->2)
TAU_CONFIDENCE = 0.75
MIN_SOURCES = 2
COUNCIL_AGREE_OK = 0.67
FUZZY_MATCH_MIN = 90      # rapidfuzz score для fuzzy-резолва

# Стадия 2->3
TAU_EDGE_ACCEPT = 0.80
