"""Tests for reliability-related configuration defaults."""
from __future__ import annotations

import json
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import httpx

import agent
import bot
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


def test_format_nudge_cap_returns_error(monkeypatch):
    nudges = 0

    def fake_chat_with_retry(messages):
        nonlocal nudges
        nudges += 1
        return "This is not a tool call or final answer."

    monkeypatch.setattr(agent, "_chat_with_retry", fake_chat_with_retry)
    logger = RunLogger(bucket_name="tds-p1-q5-logs-8463bd76cd533105")
    result = agent.solve("Question?", logger)
    assert "error" in result
    assert "format" in result["error"].lower()
    assert nudges <= 5


async def test_bot_handles_agent_exception(mocker):
    update = mocker.MagicMock()
    update.message.text = "Test question?"
    update.effective_chat.id = 123
    update.message.message_id = 1
    update.message.reply_text = mocker.AsyncMock()

    mocker.patch("agent.solve", side_effect=RuntimeError("boom"))

    await bot.handle_message(update, mocker.MagicMock())

    reply_text = update.message.reply_text.call_args[0][0]
    reply = json.loads(reply_text)
    # Grading contract: the reply is the inner value only — no
    # {"answer": ..., "log_url": ...} wrapper. On an agent crash the bot
    # surfaces {"error": ...}; the log_url lives in the run log, not the reply.
    assert "error" in reply
    assert "answer" not in reply
    assert "log_url" not in reply
