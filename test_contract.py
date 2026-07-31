"""Grading-contract tests.

The official grader (Jivraj-18/tds-p1-t2-2026-telegram-bot, the actual P1
grading pipeline) does, in grade.py:

    def extract_answer(replies):
        return json.loads(replies[-1].strip())     # the WHOLE final reply

    def grade(collected, expected):
        answer = extract_answer(collected["replies"])
        ok = answer == expected                     # exact match

and its README states: "A bot's final reply must be exactly one JSON object,
nothing else — e.g. {"state": "Assam"}." The `expected` value is the INNER
answer (e.g. {"state": "Assam"}), not a wrapper.

So the bot's Telegram reply to a JSON-requesting question must be the INNER
value only. A {"answer": ..., "log_url": ...} wrapper fails exact match and
grades every question wrong even when the inner answer is correct.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import bot


# --- grader logic, copied verbatim from the official grade.py ----------------

def _extract_answer(replies):
    if not replies:
        return None
    try:
        return json.loads(replies[-1].strip())
    except json.JSONDecodeError:
        return None


def _grade(collected, expected):
    if collected.get("status") != "ok":
        return False, collected.get("status", "not_attempted")
    answer = _extract_answer(collected.get("replies", []))
    if answer is None:
        return False, "format_error"
    ok = answer == expected
    return ok, ("ok" if ok else f"expected {expected}, got {answer}")


# --- contract: the reply is the inner value, no wrapper ----------------------

def test_final_reply_is_inner_value_only():
    """The reply for a JSON question is exactly the inner value, not wrapped."""
    assert bot._final_reply({"state": "Odisha"}) == '{"state": "Odisha"}'
    assert bot._final_reply({"median": 15}) == '{"median": 15}'
    assert bot._final_reply({"mean": 18.0}) == '{"mean": 18.0}'


def test_final_reply_has_no_wrapper_keys():
    """No `answer` or `log_url` wrapper — the grader exact-matches the whole
    reply against the inner expected value, so a wrapper grades wrong."""
    for inner in ({"state": "Odisha"}, {"median": 15}, {"values": [1, 2, 3]}):
        reply = bot._final_reply(inner)
        parsed = json.loads(reply)
        assert "answer" not in parsed, f"wrapper key leaked: {reply}"
        assert "log_url" not in parsed, f"wrapper key leaked: {reply}"
        assert parsed == inner


def test_reply_passes_official_grader():
    """Simulate the official grader on the bot's actual reply string: it must
    exact-match the inner expected value."""
    cases = [
        ({"median": 15}, {"median": 15}),
        ({"mean": 18.0}, {"mean": 18.0}),
        ({"state": "Odisha"}, {"state": "Odisha"}),
    ]
    for inner, expected in cases:
        reply = bot._final_reply(inner)
        collected = {"status": "ok", "replies": [reply]}
        ok, detail = _grade(collected, expected)
        assert ok, f"grader rejected {reply}: {detail}"


def test_reply_is_valid_single_json_object():
    """The grader's extract_answer must succeed (no prose, one JSON object)."""
    reply = bot._final_reply({"state": "Assam"})
    assert _extract_answer([reply]) == {"state": "Assam"}