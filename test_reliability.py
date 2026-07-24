"""Tests for reliability-related configuration defaults."""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import config


def test_retry_config_defaults():
    assert config.LLM_RETRY_ATTEMPTS == 1
    assert config.LLM_RETRY_BACKOFF_BASE == 2.0
    assert config.LLM_FALLBACK_MODEL == ""
