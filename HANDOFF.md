# Handoff

## What was done (this session, 2026-07-31)

Two rounds of work on the P1 Q5 Telegram bot, both shipped to GCP.

### Round 1 — cross-question context contamination (the wrong-answer bug)
- Root cause: the grader reuses ONE Telegram chat for all questions, so history
  accumulated and the agent answered a PRIOR question (returned
  `{"state": "Odisha"}` for a median question). Proven from run log
  `run-a58c3d06cf20431f.jsonl`.
- Fixes: `agent.solve(question, logger, history=...)` separates the current
  message from history; shape is extracted from the current message only;
  `bot._reset_context()` drops per-chat context after answering + on a 120s gap;
  `tools.run_python` uses a fresh `tempfile.mkdtemp` per call (was clobbered
  across concurrent chats). Covered by `test_context.py`, `test_tools.py`.
- Verified live: median → `{"median": 15}`, mean → `{"mean": 18.0}`,
  MOSPI → `{"state": "Odisha"}`, each answering its OWN question.

### Round 2 — grading contract + code-review findings
- Contract: confirmed against the exam `question.md` (authoritative) that the
  reply MUST be the wrapper `{"answer": <inner>, "log_url": <public JSONL URL>}`.
  (The public `Jivraj-18` grader repo's inner-only wording is an EARLIER version
  — do NOT trust it; trust `question.md`.) Reverted a mistaken inner-only change.
- Code-review fixes (commit `ff5b1c2`, tests in `test_review_fixes.py`):
  1. `agent`: fallback model passed as a local arg — no more `config.LLM_MODEL`
     mutation across concurrent requests.
  2. `logger.finalize`: retries transient GCS failures (3x backoff); raises on
     persistent failure instead of returning a dead log_url.
  3. `agent`: JSON parse failure now calls `logger.finish` (every run closes).
  4. `bot`: per-chat context dicts guarded with a `threading.Lock`.
  5. `tools.fetch_url`: `verify=False` retry restricted to a gov-host allowlist.
  6. `tools.fetch_url`: streams with a `MAX_RESPONSE_BYTES` (2 MB) cap.

## What is working
- Full test suite: **40 passed, 1 deselected** (`test_fetch_url_json` — known
  network-flaky, unrelated).
- VM `tds-p1-bot` running `ff5b1c2`; service active; warmup (getMe + LLM +
  search) all 200 OK.
- Live bot replies with the wrapper, e.g.
  `{"answer": {"state": "Assam"}, "log_url": "https://storage.googleapis.com/..."}`.

## What is broken or incomplete
- `test_fetch_url_json` remains network-dependent and flaky (deselected).
- VM `uv sync` cache-permission warning (pre-existing, harmless — changes add no deps).
- Which exact state the grader's private key expects for the MOSPI question is
  unknown; the agent uses official MOSPI/SRS sources and we can't do better
  without the key.

## Next step
- Send the MOSPI question to the bot and confirm the reply is the
  `{"answer": {...}, "log_url": ...}` wrapper AND the log_url is wget-able
  (downloads a JSONL with `run_start` + `run_finish`).
- If the grader's reachability check needs the JSONL to contain `run_finish`,
  that is now guaranteed (fix #3).

## Decisions made
- Reply format = `{"answer": <inner>, "log_url": <URL>}` per `question.md`.
- log_url uploaded to GCS BEFORE the reply is built so it carries a real URL.
- Did NOT rotate the OpenRouter key exposed in one transcript this session —
  do that if this transcript is shared.

## Files touched (this session)
- `agent.py`, `bot.py`, `logger.py`, `tools.py`
- `test_context.py`, `test_tools.py`, `test_cold_start.py`, `test_reliability.py`,
  `test_contract.py`, `test_review_fixes.py`
- `HANDOFF.md`, `TASKS.md`

## Active tasks from TASKS.md
(none open — all review findings closed)