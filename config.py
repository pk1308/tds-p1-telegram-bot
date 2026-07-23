"""Configuration loaded from environment / .env.

All secrets are read at runtime; nothing is hardcoded.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present (local development). On GCP the env is injected directly.
load_dotenv(Path(__file__).with_name(".env"))


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        print(f"Missing required environment variable: {key}", file=sys.stderr)
        sys.exit(1)
    return value or ""


TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)

LLM_BASE_URL = _env("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = _env("LLM_API_KEY", required=True)
LLM_MODEL = _env("LLM_MODEL", "anthropic/claude-3.5-sonnet")
LLM_TIMEOUT = float(_env("LLM_TIMEOUT", "60"))

GCS_LOG_BUCKET = _env("GCS_LOG_BUCKET", required=True)
GCS_LOG_PREFIX = _env("GCS_LOG_PREFIX", "runs/").rstrip("/") + "/"

MAX_AGENT_STEPS = int(_env("MAX_AGENT_STEPS", "15"))
PYTHON_TIMEOUT = float(_env("PYTHON_TIMEOUT", "30"))
