"""Tests for per-chat ordering + cross-chat parallelism in bot.py."""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import bot


def test_chat_lock_same_chat_returns_same_lock():
    bot._chat_locks.clear()
    a = bot._chat_lock(123)
    b = bot._chat_lock(123)
    assert a is b


def test_chat_lock_different_chats_get_different_locks():
    bot._chat_locks.clear()
    a = bot._chat_lock(123)
    b = bot._chat_lock(456)
    assert a is not b


def _make_update(mocker, msg_id, text, chat_id):
    upd = mocker.MagicMock()
    upd.message.text = text
    upd.message.message_id = msg_id
    upd.effective_chat.id = chat_id
    upd.message.reply_text = mocker.AsyncMock()
    return upd


async def test_same_chat_messages_serialize_via_lock(mocker):
    """Two messages in the same chat must not overlap: the second handler blocks
    on the per-chat lock until the first releases it. bot offloads the agent
    call to a thread (asyncio.to_thread), so the fake must be sync."""
    bot._chat_locks.clear()
    order: list[str] = []

    def fake_solve_with_retry(question, run_logger, history=None):
        order.append(f"start:{question}")
        time.sleep(0.05)
        order.append(f"end:{question}")
        return {"answer": {"state": "Assam"}}

    mocker.patch("agent.solve_with_retry", side_effect=fake_solve_with_retry)
    mocker.patch.object(bot.RunLogger, "finalize", return_value="https://x/run.jsonl")

    await asyncio.gather(
        bot.handle_message(_make_update(mocker, 1, "Q1", 1), mocker.MagicMock()),
        bot.handle_message(_make_update(mocker, 2, "Q2", 1), mocker.MagicMock()),
    )

    # Each message's start/end must not interleave with the other's.
    assert order in (["start:Q1", "end:Q1", "start:Q2", "end:Q2"],
                     ["start:Q2", "end:Q2", "start:Q1", "end:Q1"]), order


async def test_different_chats_run_in_parallel(mocker):
    """Two messages in different chats must overlap (parallel), not serialize."""
    bot._chat_locks.clear()
    overlaps: list[bool] = []
    in_flight = {"n": 0}

    def fake_solve_with_retry(question, run_logger, history=None):
        in_flight["n"] += 1
        if in_flight["n"] > 1:
            overlaps.append(True)
        time.sleep(0.05)
        in_flight["n"] -= 1
        return {"answer": {"state": "Assam"}}

    mocker.patch("agent.solve_with_retry", side_effect=fake_solve_with_retry)
    mocker.patch.object(bot.RunLogger, "finalize", return_value="https://x/run.jsonl")

    await asyncio.gather(
        bot.handle_message(_make_update(mocker, 1, "Q1", 1), mocker.MagicMock()),
        bot.handle_message(_make_update(mocker, 2, "Q2", 2), mocker.MagicMock()),
    )
    assert overlaps, "different chats were serialized instead of running in parallel"