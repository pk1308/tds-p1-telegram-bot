# TDS P1 Q5 — Data Analyst Telegram Bot

A Telegram bot that answers plain-text data-analysis questions with a single JSON object.

## What it does

- Receives a Telegram message from the grader's real user account.
- Runs a ReAct agent that can search the web, fetch URLs, and execute sandboxed Python.
- Uploads a JSONL run log to a public GCS bucket.
- Replies with exactly:

```json
{"answer": <value shaped as the question asks>, "log_url": "https://storage.googleapis.com/.../run-xxx.jsonl"}
```

## Quick start (local)

```bash
cd p1/q5-telegram-bot
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, GCS_LOG_BUCKET
uv sync
uv run python bot.py
```

## Testing the public grader pipeline

```bash
git clone https://github.com/Jivraj-18/tds-p1-t2-2026-telegram-bot.git
cd tds-p1-t2-2026-telegram-bot
cp students.example.csv students.csv
# add your bot: 23f2000254@ds.study.iitm.ac.in,https://github.com/pk1308/tds-p1-telegram-bot,<your_bot_username>
uv sync
python3 login.py          # get TELEGRAM_SESSION_STRING
python3 generate.py --students students.csv
python3 collect.py --students students.csv
python3 grade.py --students students.csv
```

## Deploy on GCP

1. Create a service account or use Application Default Credentials on the VM.
2. Ensure the GCS log bucket has `allUsers:objectViewer`.
3. Install dependencies and run `bot.py` under a systemd service or tmux.

## Files

- `bot.py` — Telegram polling entry point.
- `agent.py` — ReAct loop with the LLM and tools.
- `tools.py` — `web_search`, `fetch_url`, `run_python`.
- `logger.py` — GCS JSONL logger.
- `config.py` — environment-driven configuration.
