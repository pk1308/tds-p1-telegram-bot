# TDS P1 Q5 — Data Analyst Telegram Bot

A Telegram bot that answers plain-text data-analysis questions with a single
JSON object. A ReAct agent searches the web, fetches URLs, and runs sandboxed
Python to compute the answer; each run is logged to a public GCS bucket so the
grader can download it.

## Reply contract

Per the exam `question.md`, the bot's final reply is exactly one JSON object:

```json
{"answer": <value shaped as the question asks>, "log_url": "<public URL to the run's JSONL log>"}
```

For example:

```json
{"answer": {"state": "Assam"}, "log_url": "https://storage.googleapis.com/tds-p1-q5-logs-…/run-xxxx.jsonl"}
```

The grader extracts the `answer` field for exact-match against its private key
and checks that `log_url` is a reachable, wget-able JSONL. `log_url` is uploaded
to GCS *before* the reply is built, so it always carries a real URL.

## How it works

- `bot.py` polls Telegram, runs the agent per message, uploads the run log to
  GCS, and replies.
- `agent.py` is a prompt-driven ReAct loop (works with any OpenAI-compatible
  endpoint, no native tool-calling needed). It calls one tool per step and ends
  with `Final Answer: <JSON>`.
- `tools.py` — `web_search` (DuckDuckGo), `fetch_url` (streams with a byte cap;
  CSV/JSON/PDF detection), `run_python` (restricted subprocess, isolated
  workdir per call).
- `logger.py` — JSONL run log to a public GCS bucket.

## Reliability

- **Per-chat context reset.** The grader reuses one Telegram chat for every
  question, so the bot resets a chat's context after answering and on a 120s
  quiet gap. The agent answers the *current* message; prior messages in the same
  question are passed as context only. Shape is extracted from the current
  message so a prior question's template can't leak in.
- **LLM retry + fallback.** Transient failures (timeout, connect, 5xx) retry
  with exponential backoff. A fallback model (`LLM_FALLBACK_MODEL`) is passed as
  a local argument — it never mutates the global model, so concurrent requests
  don't see each other's model.
- **Deadline nudge.** Near the step cap the agent is pushed to commit its best
  `Final Answer` instead of silently exhausting steps (the main cold-start fix).
- **Warmup + keepalive.** On startup and every 5 min the LLM endpoint, DNS, and
  search are pre-warmed so a quiet VM doesn't cold-start into a timeout.
- **Every run closes.** `run_start` and `run_finish` are always logged, even on
  parse failure or crash; the bot still replies with valid JSON and a log URL.
- **Isolated sandbox.** Each `run_python` call gets its own `tempfile.mkdtemp`
  workdir so concurrent chats can't clobber each other's code file.
- **Bounded fetch.** `fetch_url` streams with a 2 MB cap and only disables TLS
  verification for a government-host allowlist (some Indian govt sites serve
  broken certs) — never for arbitrary URLs.

## Quick start (local)

```bash
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, LLM_*, GCS_LOG_BUCKET
uv sync
uv run python bot.py
```

### Environment

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from `@BotFather`. |
| `LLM_BASE_URL` | OpenAI-compatible chat endpoint (OpenRouter recommended). |
| `LLM_API_KEY` | Key for that endpoint. |
| `LLM_MODEL` | Primary model, e.g. `openai/gpt-4o`. |
| `LLM_FALLBACK_MODEL` | Optional fallback if the primary keeps failing. |
| `LLM_RETRY_ATTEMPTS` / `LLM_RETRY_BACKOFF_BASE` | Retry count / backoff base. |
| `GCS_LOG_BUCKET` / `GCS_LOG_PREFIX` | Public bucket (allUsers:objectViewer) and object prefix. |
| `GOOGLE_CLOUD_PROJECT` | GCP project for Application Default Credentials. |

## Tests

```bash
uv run pytest --deselect test_tools.py::test_fetch_url_json
```

`test_fetch_url_json` is a live network test and is deselected by default.
The rest are hermetic: `test_context.py` (cross-question context), `test_cold_start.py`
(deadline nudge / retry / warmup), `test_reliability.py` (retry + crash handling),
`test_action_parsing.py` (tool-call parsing), `test_contract.py` (reply shape),
`test_review_fixes.py` (concurrency / GCS retry / TLS / parse-finish / byte cap),
`test_tools.py` (sandbox isolation).

## Test against the public grader pipeline

The public grader is [`Jivraj-18/tds-p1-t2-2026-telegram-bot`](https://github.com/Jivraj-18/tds-p1-t2-2026-telegram-bot).
Note: its `evals/questions.json` asks for the inner value only (`{"state": "…"}`) —
an **earlier** version. The real exam (`question.md`) requires the
`{"answer": …, "log_url": …}` wrapper above; trust the exam wording, not that repo.

```bash
git clone https://github.com/Jivraj-18/tds-p1-t2-2026-telegram-bot.git
cd tds-p1-t2-2026-telegram-bot
cp students.example.csv students.csv   # add your bot's @username
uv sync
python3 login.py               # get TELEGRAM_SESSION_STRING
python3 generate.py --students students.csv
python3 collect.py --students students.csv
python3 grade.py --students students.csv
```

## Deploy on GCP

1. On the VM, use Application Default Credentials (`gcloud auth application-default
   login` for a service account, or the VM's attached service account).
2. Ensure the GCS log bucket grants `allUsers: objectViewer`.
3. `uv sync` and run `bot.py` under a systemd service (see `deploy/` for a unit
   file and install script).

## Files

- `bot.py` — Telegram polling, context reset, reply assembly.
- `agent.py` — ReAct loop, deadline nudge, retry/fallback.
- `tools.py` — `web_search`, `fetch_url`, `run_python`.
- `logger.py` — GCS JSONL run logger (retry on transient failure).
- `config.py` — environment-driven configuration.
- `python_runner.py` — restricted-BUILTINS subprocess runner.