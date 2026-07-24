"""Tests for reliability-related configuration defaults."""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import httpx

import agent
import config
from logger import RunLogger


def test_retry_config_defaults():
    assert config.LLM_RETRY_ATTEMPTS == 1
    assert config.LLM_RETRY_BACKOFF_BASE == 2.0
    assert config.LLM_FALLBACK_MODEL == ""


def test_logger_start_and_finish(mocker):
    logger = RunLogger(bucket_name="tds-p1-q5-logs-8463bd76cd533105")
    logger.start("Which state?", "openai/gpt-4o")
    logger.finish({"state": "Odisha"}, 5, 1234.5, "success")

    events = logger._lines
    assert events[0]["event"] == "run_start"
    assert events[0]["question"] == "Which state?"
    assert events[0]["model"] == "openai/gpt-4o"
    assert events[-1]["event"] == "run_finish"
    assert events[-1]["answer"] == {"state": "Odisha"}
    assert events[-1]["steps"] == 5
    assert events[-1]["duration_ms"] == 1234.5
    assert events[-1]["status"] == "success"
    assert "error" not in events[-1]


def test_chat_with_retry_succeeds_on_second_call(monkeypatch):
    calls = []

    def fake_chat(messages):
        calls.append(messages)
        if len(calls) == 1:
            raise httpx.TimeoutException("first call timeout")
        return "Final Answer: {\"state\": \"Odisha\"}"

    monkeypatch.setattr(agent, "_chat", fake_chat)
    result = agent._chat_with_retry([{"role": "user", "content": "hi"}])
    assert result == "Final Answer: {\"state\": \"Odisha\"}"
    assert len(calls) == 2
