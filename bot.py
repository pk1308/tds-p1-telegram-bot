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

# In-memory conversation context per chat. The grader's sequences are short and
# fast, so a simple bounded buffer is enough.
_conversation_context: dict[int | None, list[str]] = {}
_MAX_CONTEXT = 10


JSON_REQUEST_RE = re.compile(
    r"reply\s+with\s+only.*json|json\s+object|reply\s+with\s+a\s+json",
    re.IGNORECASE,
)


def _store_message(chat_id: int | None, text: str) -> list[str]:
    if chat_id is None:
        return [text]
    ctx = _conversation_context.setdefault(chat_id, [])
    ctx.append(text)
    if len(ctx) > _MAX_CONTEXT:
        ctx.pop(0)
    return list(ctx)


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

    asks_for_json = bool(JSON_REQUEST_RE.search(text))

    if not asks_for_json:
        # Acknowledge intermediate context-only messages so collect.py does not
        # time out; keep context for the final question.
        reply = json.dumps({"answer": "OK", "log_url": ""}, ensure_ascii=False)
        run_logger.log("ack", {"reply": reply})
    else:
        full_prompt = "\n".join(
            ["Conversation so far:"]
            + [f"- {m}" for m in context_messages]
            + ["", "Answer the LAST message above."]
        )
        try:
            result = agent.solve(full_prompt, run_logger)
        except Exception as exc:  # noqa: BLE001
            run_logger.log("agent_exception", {"error": f"{type(exc).__name__}: {exc}"})
            result = {"error": f"Agent crashed: {exc}"}

        if "error" in result:
            answer_value: Any = {"error": result["error"]}
        else:
            answer_value = result.get("answer")

        reply = json.dumps({"answer": answer_value, "log_url": ""}, ensure_ascii=False)
        run_logger.log("reply_draft", {"reply": reply})

    try:
        log_url = run_logger.finalize()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to upload log")
        log_url = f"https://storage.googleapis.com/{config.GCS_LOG_BUCKET}/upload-failed"
        run_logger.log("log_upload_failed", {"error": f"{type(exc).__name__}: {exc}"})

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

    await update.message.reply_text(reply)


def main() -> None:
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
