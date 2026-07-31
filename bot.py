"""Telegram bot entry point.

Receives messages, runs the data-analysis agent, uploads a JSONL log to GCS,
and replies with exactly one JSON object: {"answer": ..., "log_url": ...}.

Multi-turn exchanges: the grader sends a short sequence of messages and waits
for a reply after each. We acknowledge intermediate context-only messages
with "OK", and only emit the final JSON answer when the message asks for it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import agent
import config
from logger import RunLogger

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory conversation context per chat. The grader reuses ONE Telegram chat
# for every question, so without an explicit reset the agent would see every
# prior question as context and answer a stale one. We reset a chat's context
# when (a) we just sent a JSON answer — that ends the current question — and
# (b) a long quiet gap precedes a new message (belt-and-suspenders, e.g. if a
# prior answer branch errored before resetting).
_conversation_context: dict[int | None, list[str]] = {}
_last_seen: dict[int | None, float] = {}
_MAX_CONTEXT = 10
_CONTEXT_RESET_SECONDS = 120.0


JSON_REQUEST_RE = re.compile(
    r"json\s*(?:object|value|response|answer|reply)|reply\s+with\s+(?:only\s+)?(?:a\s+)?json|answer\s+with\s+json|respond\s+with\s+json|ONLY\s+(?:this\s+)?JSON",
    re.IGNORECASE,
)


def _looks_like_json_request(text: str) -> bool:
    """Detect whether the user wants a JSON answer."""
    if JSON_REQUEST_RE.search(text):
        return True
    # If the message contains a JSON literal and ends with a question, it's likely the final ask.
    has_json_literal = bool(re.search(r"\{[^{}]*\}", text))
    ends_with_question = text.strip().endswith("?") or "?" in text
    return has_json_literal and ends_with_question


def _store_message(chat_id: int | None, text: str, *, now: float | None = None) -> list[str]:
    """Append the incoming message to this chat's context and return the full list.

    If the previous message in this chat was more than ``_CONTEXT_RESET_SECONDS``
    ago, the context is dropped first — that means a new question has started,
    not a continuation of the last one. ``now`` is injectable for tests.
    """
    if chat_id is None:
        return [text]
    if now is None:
        now = time.monotonic()
    last = _last_seen.get(chat_id)
    if last is not None and now - last > _CONTEXT_RESET_SECONDS:
        _conversation_context[chat_id] = []
    _last_seen[chat_id] = now
    ctx = _conversation_context.setdefault(chat_id, [])
    ctx.append(text)
    if len(ctx) > _MAX_CONTEXT:
        ctx.pop(0)
    return list(ctx)


def _reset_context(chat_id: int | None) -> None:
    """Drop this chat's accumulated context — call after sending a JSON answer,
    since the grader treats our JSON reply as the end of the current question.
    """
    if chat_id is not None:
        _conversation_context.pop(chat_id, None)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text
    chat_id = update.effective_chat.id if update.effective_chat else None
    message_id = update.message.message_id
    context_messages = _store_message(chat_id, text)

    run_logger = RunLogger()
    run_logger.log(
        "message_received",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "context_length": len(context_messages),
        },
    )

    asks_for_json = _looks_like_json_request(text)
    # Always answer the first/only message. For multi-turn, answer only the
    # messages that explicitly ask for a JSON reply; ack others with "OK".
    if len(context_messages) == 1 or asks_for_json:
        # The current message is the question to answer; prior messages in the
        # SAME question (after the per-question reset) are multi-turn context.
        current_message = text
        history = context_messages[:-1]
        run_logger.start(current_message, config.LLM_MODEL)
        try:
            result = agent.solve_with_retry(current_message, run_logger, history=history)
        except Exception as exc:  # noqa: BLE001
            run_logger.log("bot_exception", {"error": f"{type(exc).__name__}: {exc}"})
            result = {"error": f"Agent crashed: {exc}"}

        if "error" in result:
            answer_value: Any = {"error": result["error"]}
        else:
            answer_value = result.get("answer")

        reply = json.dumps({"answer": answer_value, "log_url": ""}, ensure_ascii=False)
        run_logger.log("reply_draft", {"reply": reply})
    else:
        run_logger.start(text, config.LLM_MODEL)
        # Acknowledge intermediate context-only messages so collect.py does not
        # time out; keep context for the final question.
        reply = json.dumps({"answer": "OK", "log_url": ""}, ensure_ascii=False)
        run_logger.log("ack", {"reply": reply})

    try:
        log_url = run_logger.finalize()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to upload log")
        run_logger.log("log_upload_failed", {"error": f"{type(exc).__name__}: {exc}"})
        log_url = f"https://storage.googleapis.com/{config.GCS_LOG_BUCKET}/upload-failed"

    # Patch the log_url into the final reply.
    try:
        reply_obj = json.loads(reply)
        reply_obj["log_url"] = log_url
        reply = json.dumps(reply_obj, ensure_ascii=False)
    except json.JSONDecodeError:
        reply = json.dumps({"answer": reply, "log_url": log_url}, ensure_ascii=False)

    run_logger.log("reply_sent", {"reply": reply})
    # Best-effort re-upload with the final reply included.
    try:
        run_logger.finalize()
    except Exception:  # noqa: BLE001
        pass

    # We answered: the grader treats this JSON reply as the end of the current
    # question, so drop this chat's context so the next message starts fresh
    # and cannot inherit a prior question's text.
    if len(context_messages) == 1 or asks_for_json:
        _reset_context(chat_id)

    await update.message.reply_text(reply)


async def _post_init(application: Application) -> None:
    """Pre-warm the LLM endpoint, DNS, and tool path on a cold start."""
    logger.info("post_init: warming up LLM + tools")
    agent.warmup()


async def _keepalive(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic warm-up so a quiet VM doesn't go cold between messages."""
    agent.warmup()


def main() -> None:
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Keep the endpoint/DNS warm every 5 min so a cold start doesn't cause a
    # step-exhaustion timeout on the first message after a quiet period.
    if application.job_queue is not None:
        application.job_queue.run_repeating(_keepalive, interval=300, first=300)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
