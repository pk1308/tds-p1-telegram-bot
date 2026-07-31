"""Grading-contract tests, from the actual exam (question.md).

The exam question wording is:

    Which state has the highest maternal mortality rate based on MOSPI data?
    Reply with ONLY this JSON object and nothing else:
    {"answer": {"state": "<state name>"}, "log_url": "<public wget-able URL to your agent's JSONL log>"}

So the bot's final reply must be the wrapper
    {"answer": <inner answer shaped as the question asks>, "log_url": <URL>}
— not the inner value alone. The grader extracts the `answer` field for
exact-match against the expected inner value and checks `log_url` is a
reachable URL; it does not exact-match the whole object (log_url varies).

NOTE: the public Jivraj-18/tds-p1-t2-2026-telegram-bot repo's evals/questions.json
asks for inner-only ("Reply with ONLY a JSON object like {"state": "<state name>"}")
— an EARLIER version. The exam question.md (wrapper + log_url) is authoritative.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import bot

LOG_URL = "https://storage.googleapis.com/tds-p1-q5-logs-8463bd76cd533105/run-test.jsonl"


# --- the grader extracts `answer` and exact-matches it against expected --------

def _extract_answer_field(replies):
    """Real grader: parse the reply, then take the `answer` field (the graded
    value). log_url is checked separately for reachability, not exact-matched."""
    if not replies:
        return None
    try:
        obj = json.loads(replies[-1].strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "answer" not in obj:
        return None
    return obj["answer"]


def _grade(collected, expected):
    if collected.get("status") != "ok":
        return False, collected.get("status", "not_attempted")
    answer = _extract_answer_field(collected.get("replies", []))
    if answer is None:
        return False, "format_error"
    ok = answer == expected
    return ok, ("ok" if ok else f"expected {expected}, got {answer}")


# --- contract: the reply is the {answer, log_url} wrapper ---------------------

def test_final_reply_wraps_answer_and_log_url():
    """Reply shape matches question.md exactly."""
    reply = bot._final_reply({"state": "Odisha"}, LOG_URL)
    parsed = json.loads(reply)
    assert parsed == {"answer": {"state": "Odisha"}, "log_url": LOG_URL}


def test_final_reply_answer_field_is_inner_value():
    """The `answer` field carries the inner value, shaped as the question asks."""
    for inner in ({"state": "Odisha"}, {"median": 15}, {"values": [1, 2, 3]}):
        parsed = json.loads(bot._final_reply(inner, LOG_URL))
        assert parsed["answer"] == inner


def test_final_reply_has_log_url():
    """log_url must be present (the grader checks it is a reachable URL)."""
    parsed = json.loads(bot._final_reply({"state": "Assam"}, LOG_URL))
    assert parsed["log_url"] == LOG_URL


def test_reply_passes_grader_answer_extraction():
    """Simulate the grader: extract `answer` from the reply and exact-match it
    against the expected inner value."""
    cases = [
        ({"median": 15}, {"median": 15}),
        ({"mean": 18.0}, {"mean": 18.0}),
        ({"state": "Odisha"}, {"state": "Odisha"}),
    ]
    for inner, expected in cases:
        reply = bot._final_reply(inner, LOG_URL)
        collected = {"status": "ok", "replies": [reply]}
        ok, detail = _grade(collected, expected)
        assert ok, f"grader rejected {reply}: {detail}"


def test_reply_is_valid_single_json_object():
    """The reply parses as one JSON object (no prose around it)."""
    reply = bot._final_reply({"state": "Assam"}, LOG_URL)
    obj = json.loads(reply)
    assert isinstance(obj, dict) and set(obj) == {"answer", "log_url"}