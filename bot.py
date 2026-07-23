"""Telegram bot entry point.

Receives messages, runs the data-analysis agent, uploads a JSONL log to GCS,
and replies with exactly one JSON object: {"answer": ..., "log_url": ...}.
"""
from __future__ import annotations

import asyncio
import json
import logging
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text
    chat_id = update.effective_chat.id if update.effective_chat else None
    message_id = update.message.message_id

    run_logger = RunLogger()
    run_logger.log(
        "message_received",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        },
    )

    try:
        result = agent.solve(text, run_logger)
    except Exception as exc:  # noqa: BLE001
        run_logger.log("agent_exception", {"error": f"{type(exc).__name__}: {exc}"})
        result = {"error": f"Agent crashed: {exc}"}

    if "error" in result:
        answer_value: Any = {"error": result["error"]}
    else:
        answer_value = result.get("answer")

    try:
        log_url = run_logger.finalize()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to upload log")
        log_url = f"https://storage.googleapis.com/{config.GCS_LOG_BUCKET}/upload-failed"
        run_logger.log("log_upload_failed", {"error": f"{type(exc).__name__}: {exc}"})

    reply = json.dumps({"answer": answer_value, "log_url": log_url}, ensure_ascii=False)
    run_logger.log("reply_sent", {"reply": reply})

    # Re-upload with the reply included (best effort; original URL stays valid).
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
