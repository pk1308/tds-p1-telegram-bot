# TDS P1 Q5 Telegram Bot — Reliability & Presentation Polish

Date: 2026-07-24
Scope: Safe, non-invasive improvements to the already-deployed P1 Q5 data-analysis Telegram bot.

## Goal

Strengthen the bot against transient failures and make its GCS logs more useful for the grader, without changing the reply format or core agent behavior that currently passes the sample question.

## What stays the same

- Reply format: `{"answer": ..., "log_url": ...}`
- Tool contracts: `web_search`, `fetch_url`, `run_python`
- Agent loop structure and system prompt
- Deployed VM service / systemd setup
- OpenRouter `openai/gpt-4o` default model

## What changes

### 1. Richer JSONL logs (`logger.py`)

- `RunLogger.start(question, model)` emits a `run_start` header containing:
  - `ts`, `run_id`, `event: run_start`
  - `question`, `model`, `llm_base_url`
- `RunLogger.finish(answer, steps, duration_ms, status)` emits a `run_finish` footer containing:
  - `ts`, `run_id`, `event: run_finish`
  - `answer`, `steps`, `duration_ms`
  - `status`: one of `success`, `llm_error`, `timeout`, `exception`, `upload_failed`
  - `error`: short error message when status is not `success`
- Existing per-step events (`llm_response`, `tool_call`, etc.) are preserved and gain a `duration_ms` field for LLM calls.

### 2. LLM retry with backoff (`agent.py`)

- New helper `_chat_with_retry(messages, attempts)`.
- Retries on:
  - `httpx.TimeoutException`
  - `httpx.ConnectError`
  - HTTP 5xx responses
  - Any `httpx.HTTPStatusError` where status >= 500
- Backoff: `2^(attempt)` seconds, capped at 8 s.
- Each retry attempt is logged as `llm_retry` with `attempt`, `wait_ms`, and `error`.
- If all retries fail, the agent returns `{"error": "LLM unavailable after N retries: <reason>"}`.

### 3. Graceful top-level crash handling (`bot.py`)

- Wrap `agent.solve` in a try/except that catches any unhandled exception.
- On exception:
  - Log `bot_exception` with the full traceback.
  - Upload the partial log.
  - Reply with valid JSON:
    ```json
    {"answer": {"error": "Agent crashed: <short reason>"}, "log_url": "..."}
    ```
- This guarantees the Telegram handler never silently dies and always gives the grader a debug URL.

### 4. Agent loop safety improvements (`agent.py`)

- Count consecutive format nudges. If the model needs more than 3 nudges in one run, stop and return `{"error": "Agent could not follow tool format after N nudges"}` to avoid wasted loops.
- On `MAX_AGENT_STEPS` timeout, include a short `last_observation_summary` in the error log so the grader sees what the agent was stuck on.

### 5. New configuration knobs (`config.py`)

Optional environment variables with safe defaults:

- `LLM_RETRY_ATTEMPTS` — default `1` (one retry, i.e. two total calls)
- `LLM_RETRY_BACKOFF_BASE` — default `2.0`
- `LLM_FALLBACK_MODEL` — default empty; if set and the primary model fails all retries, one final attempt is made with the fallback before giving up.

### 6. Tests (`test_reliability.py`)

- `test_llm_retry_succeeds_on_second_call`: mock `_chat` to fail once then succeed; assert retry is logged and answer is returned.
- `test_llm_retry_exhausted_returns_error`: mock `_chat` to always fail; assert graceful error answer and `llm_error` status.
- `test_run_logger_header_and_footer`: create a logger, call `start`/`finish`, assert both events present and footer status is `success`.
- `test_bot_handles_agent_exception`: monkey-patch `agent.solve` to raise; assert bot replies with JSON containing `error` and a non-empty `log_url`.

## Data flow

```
Telegram message
  -> bot.handle_message
    -> RunLogger.start(question, model)
    -> agent.solve
      -> _chat_with_retry (log duration_ms, retry events)
      -> tool calls (existing events)
      -> Final Answer / error
    -> RunLogger.finish(answer, steps, duration_ms, status)
    -> RunLogger.finalize -> GCS log_url
    -> reply JSON
```

## Error states and responses

| Failure | Bot reply `answer` | Log status |
|---|---|---|
| LLM succeeds | whatever the agent computes | `success` |
| LLM transient failure then succeeds | whatever the agent computes | `success` with `llm_retry` events |
| LLM fails all retries | `{"error": "LLM unavailable after N retries: ..."}` | `llm_error` |
| Agent times out (steps) | `{"error": "Agent did not produce a Final Answer within N steps"}` | `timeout` |
| Agent cannot follow format | `{"error": "Agent could not follow tool format after N nudges"}` | `timeout` |
| Unhandled exception | `{"error": "Agent crashed: ..."}` | `exception` |
| GCS upload fails | original answer | `upload_failed` with error text |

## Risks and mitigation

- **Risk**: retry increases total latency. Mitigation: only one retry, short backoff cap, 60 s total LLM timeout unchanged.
- **Risk**: fallback model changes answer quality. Mitigation: disabled by default; only used if primary model repeatedly fails.
- **Risk**: touching code close to submission introduces bugs. Mitigation: keep changes additive, run all existing tests after each edit, and re-run the live MOSPI test before submitting.

## Success criteria

1. All existing tests still pass: `uv run pytest -q` returns green.
2. New reliability tests pass.
3. Local MOSPI sample test returns `{"answer": {"state": "Odisha"}}` in under 20 s.
4. Live test via Telegram returns valid JSON and the log URL downloads a JSONL file with `run_start` and `run_finish` events.
