"""Regression tests for cross-question context contamination.

Bug: the grader reuses one Telegram chat for every question, so chat history
accumulates across questions. The bot used to feed the whole history as one
blob and the agent answered a PRIOR question (e.g. returned {"state": "Odisha"}
for a median question). These tests pin the fix: the current message is
separated from history, shape is extracted from the current message only, and
per-chat context resets between questions.
"""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import agent
import bot


class _CapLog:
    def __init__(self):
        self.events = []

    def start(self, *a, **k):
        pass

    def log(self, event, data):
        self.events.append((event, data))

    def finish(self, *a, **k):
        pass


# --- agent: current message vs history -------------------------------------

def test_shape_comes_from_current_message_not_history(monkeypatch):
    """A prior question's JSON template must NOT leak into the current run.

    The median template {"median": <number>} is not JSON-parsable, so the
    requested shape must be None. Before the fix, _extract_requested_shape
    scanned the whole conversation blob and returned the stale
    {"state": "<state name>"} from the MOSPI question, misdirecting the agent.
    """
    monkeypatch.setattr(agent, "_chat_with_retry", lambda msgs: 'Final Answer: {"median": 15}')
    monkeypatch.setattr(agent, "_call_tool", lambda name, args: "ok")

    current = (
        "The daily sales (in units) for a week are: 12, 15, 9, 20, 18, 25, 14. "
        'What is the median? Reply with ONLY a JSON object like {"median": <number>}'
    )
    history = [
        'Which state has the highest maternal mortality rate based on MOSPI data? '
        'Reply with ONLY a JSON object like {"state": "<state name>"}'
    ]

    lg = _CapLog()
    result = agent.solve(current, lg, history=history)

    assert result == {"answer": {"median": 15}}
    shape = [d for e, d in lg.events if e == "agent_start"][0]["requested_shape"]
    assert shape is None, f"stale shape leaked from history: {shape!r}"


def test_solve_passes_current_message_as_primary(monkeypatch):
    """The LLM must see the current question as the message to answer, with
    history labelled as context only."""
    sent = []

    def fake_chat(messages):
        sent.append(messages)
        return 'Final Answer: {"median": 15}'

    monkeypatch.setattr(agent, "_chat_with_retry", fake_chat)
    monkeypatch.setattr(agent, "_call_tool", lambda name, args: "ok")

    current = "What is the median of 12, 15, 9? Reply {\"median\": <number>}"
    history = ["Earlier context message about MOSPI data."]
    agent.solve(current, _CapLog(), history=history)

    user_content = sent[0][-1]["content"]
    assert "Current message" in user_content
    assert "median" in user_content
    # history is present but only as context
    assert "MOSPI" in user_content


def test_solve_without_history_still_works(monkeypatch):
    """Single-message question (no history) must still answer correctly."""
    monkeypatch.setattr(agent, "_chat_with_retry", lambda msgs: 'Final Answer: {"state": "Assam"}')
    monkeypatch.setattr(agent, "_call_tool", lambda name, args: "ok")
    result = agent.solve(
        'Which state? Reply {"state": "<x>"}', _CapLog()
    )
    assert result == {"answer": {"state": "Assam"}}


# --- bot: per-chat context reset -------------------------------------------

def test_store_message_keeps_turns_within_a_question():
    bot._conversation_context.clear()
    bot._last_seen.clear()
    ctx = bot._store_message(123, "ctx: here is some data", now=0.0)
    assert ctx == ["ctx: here is some data"]
    ctx = bot._store_message(123, "now answer it", now=1.0)
    assert ctx == ["ctx: here is some data", "now answer it"]


def test_store_message_resets_after_a_long_gap():
    """A new question after a quiet period must start fresh, not inherit the
    prior question's messages."""
    bot._conversation_context.clear()
    bot._last_seen.clear()
    bot._store_message(123, "old question Q1", now=0.0)
    # gap exceeds the reset threshold -> drop Q1
    ctx = bot._store_message(123, "new question Q2", now=10_000.0)
    assert ctx == ["new question Q2"]


def test_reset_context_drops_history():
    bot._conversation_context.clear()
    bot._last_seen.clear()
    bot._store_message(123, "Q1", now=0.0)
    bot._reset_context(123)
    assert 123 not in bot._conversation_context


def test_store_message_none_chat_is_stateless():
    bot._conversation_context.clear()
    bot._last_seen.clear()
    ctx = bot._store_message(None, "hello", now=0.0)
    assert ctx == ["hello"]