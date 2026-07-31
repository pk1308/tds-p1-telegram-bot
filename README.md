# TDS P1 Q5 — Data Analyst Telegram Bot

A Telegram bot that answers plain-text data-analysis questions with a single
JSON object. A ReAct agent writes real Python (pandas/numpy/requests/bs4) to
fetch and compute the answer in one shot; each run is logged to a public GCS
bucket so the grader can download it.

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

The agent engine is a graft of the proven reference bot
(`23f1002539/tds-data-analyst-bot`) onto our own infrastructure (per-run GCS
logging, per-chat context reset, warmup, retry/fallback, contract wrapper).

- `bot.py` polls Telegram, runs the agent per message, uploads the run log to
  GCS, and replies. Updates run concurrently with **per-chat ordering**: one
  chat's messages are handled strictly in order (store → solve → reply) while
  different chats run in parallel, so one slow chat never blocks another. The
  blocking agent + GCS work runs in a thread (`asyncio.to_thread`) so the event
  loop is never blocked.
- `agent.py` is a prompt-driven ReAct loop that works with any OpenAI-compatible
  endpoint (no native tool-calling). The model emits fenced ```python blocks
  (executed by the sandbox) and finishes with `FINAL_ANSWER: <json>`. The answer
  is shape-enforced against the JSON template extracted from the question so no
  extra keys leak into the exact-matched graded value. If a model mistakenly
  wraps its answer in `{"answer": ...}`, it is unwrapped before enforcement.
- `python_runner.py` — the sandbox: runs the agent's code in a subprocess with
  the full data-analysis stack (pandas, numpy, requests, bs4, pypdf) plus our
  `web_search`/`fetch_url` helpers and the stdlib. Containment = a fresh temp
  workdir per call, a hard timeout, and the non-root service user. We do NOT
  restrict builtins/imports — that restriction was the core capability gap (the
  agent could not do real data work without numpy/requests).
- `tools.py` — `web_search` (DuckDuckGo), `fetch_url` (streams with a 2 MB cap;
  CSV/JSON/PDF detection; TLS verify=False only for a gov-host allowlist),
  `run_python` (spawns the sandbox in an isolated workdir per call).
- `logger.py` — JSONL run log to a public GCS bucket (retries transient
  failures; raises rather than fabricating a dead log_url).

## Model

`google/gemini-2.5-flash` via OpenRouter is the default (`LLM_MODEL`). It is
fast, cheap, and follows the no-wrap + shape-fidelity prompt reliably.
`temperature: 0.0`; reasoning models (gpt-5/gemini/o1/o3) get
`reasoning: {effort: low}`. Any OpenAI-compatible model works; set
`LLM_FALLBACK_MODEL` for a secondary model used only if the primary keeps
failing on transient errors.

## Reliability

- **Per-chat context reset.** The grader reuses one Telegram chat for every
  question, so the bot resets a chat's context after answering and on a 120s
  quiet gap. The agent answers the *current* message; prior messages in the
  same question are passed as context only. The template is extracted from the
  current message so a prior question's template can't leak in.
- **LLM retry + fallback.** Transient failures (timeout, connect, 5xx) retry
  with exponential backoff. The fallback model is passed as a local argument —
  it never mutates the global model, so concurrent requests don't see each
  other's model.
- **Deadline nudge.** Near the step cap the agent is pushed to commit its best
  `FINAL_ANSWER` instead of silently exhausting steps.
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
| `LLM_MODEL` | Primary model, e.g. `google/gemini-2.5-flash`. |
| `LLM_FALLBACK_MODEL` | Optional fallback if the primary keeps failing. |
| `LLM_RETRY_ATTEMPTS` / `LLM_RETRY_BACKOFF_BASE` | Retry count / backoff base. |
| `GCS_LOG_BUCKET` / `GCS_LOG_PREFIX` | Public bucket (allUsers:objectViewer) and object prefix. |
| `GOOGLE_CLOUD_PROJECT` | GCP project for Application Default Credentials. |

## Tests

```bash
uv run pytest --deselect test_tools.py::test_fetch_url_json
```

`test_fetch_url_json` is a live network test and is deselected by default. The
rest are hermetic:

- `test_agent_protocol.py` — fenced-python + `FINAL_ANSWER` parsing, template
  extraction, shape enforcement, and the mistaken-wrapper unwrap safety net.
- `test_concurrency.py` — per-chat serialization + cross-chat parallelism.
- `test_context.py` — cross-question context isolation.
- `test_cold_start.py` — deadline nudge / retry-on-timeout / warmup.
- `test_reliability.py` — retry + crash handling.
- `test_contract.py` — reply shape vs. the exam `question.md`.
- `test_review_fixes.py` — concurrency / GCS retry / TLS / parse-finish / byte cap.
- `test_tools.py` — sandbox data libs + isolation.

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

- `bot.py` — Telegram polling, per-chat ordering/parallelism, context reset,
  reply assembly.
- `agent.py` — ReAct loop (fenced-python + `FINAL_ANSWER`), shape enforcement,
  deadline nudge, retry/fallback.
- `tools.py` — `web_search`, `fetch_url`, `run_python`.
- `python_runner.py` — subprocess sandbox with the data-analysis stack.
- `logger.py` — GCS JSONL run logger (retry on transient failure).
- `config.py` — environment-driven configuration.