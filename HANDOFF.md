# Handoff

## What was done (this session, 2026-07-31)

### Round 3 — full graft of the reference agent engine

The reference bot (`23f1002539/tds-data-analyst-bot`) gives correct, well-shaped
replies; ours did not. Root cause: our `Action: tool(...)` protocol + restricted
sandbox (no numpy/requests) + `openai/gpt-4o` + 15 steps + serial per-message
handling. The user chose a **full graft** of the reference's proven engine onto
our infrastructure. Also switched the model and fixed per-chat parallelism
(ours was slower).

Changes (all test-first, 59 passed / 1 deselected):

- **`agent.py` rewritten** to the reference protocol:
  - Fenced ```python blocks (executed by the sandbox) + `FINAL_ANSWER: <json>`.
  - `_extract_template` (balanced-brace scan; returns the inner template from
    the exam `{"answer": <t>, "log_url": ...}` wrapper).
  - `_enforce_shape` (recursive key-filter so no extra keys leak into the
    exact-matched graded value).
  - `_extract_final` (accepts both `FINAL_ANSWER:` and legacy `Final Answer:`).
  - **`_unwrap_answer` safety net** (NEW, not in the reference): if the model
    mistakenly emits the `{"answer": ..., "log_url": ...}` wrapper, unwrap it
    before enforcement. This is *essential* — even `gemini-2.5-flash` emitted
    `FINAL_ANSWER: {"answer": {"median": 15.0}}` in the live smoke test; without
    unwrapping, `_enforce_shape` would produce `{}` and fail grading.
  - `temperature: 0.0`; `reasoning: {effort: low}` for gpt-5/gemini/o1/o3.
  - Kept: `_chat`/`_chat_with_retry` (model passed as local arg, no global
    mutation), `solve`/`solve_with_retry`/`warmup` signatures, logger
    `run_start`/`run_finish` on every path, history/context separation, deadline
    nudge, format-nudge cap, parse-error → run_finish.
- **`python_runner.py` rewritten** — unrestricted exec with a preamble exposing
  pandas/numpy/requests/bs4/pypdf + stdlib + our `web_search`/`fetch_url`
  helpers. Containment = temp workdir + timeout + non-root service user (no
  restricted builtins — that was the capability gap). `tools.run_python`
  unchanged (already isolated workdir + subprocess).
- **`config.py` / `.env.example`** — default `LLM_MODEL` → `google/gemini-2.5-flash`.
- **`bot.py`** — `concurrent_updates=True` + per-chat `asyncio.Lock` +
  `asyncio.to_thread` for the blocking solve + GCS finalize. Same-chat messages
  serialize (store → solve → reply); different chats run in parallel. This fixes
  the latency regression (the old sync handler blocked the event loop, so the
  grader's ~5 concurrent chats were serialized).
- Added deps: `numpy`, `requests`, `beautifulsoup4` (were installed but not
  declared; `uv sync` on the VM now installs them).
- Removed dead `test_action_parsing.py` (tested the removed `_find_tool_call`);
  new protocol covered by `test_agent_protocol.py`. Logged in `CHANGES.md`.

### Live smoke verified
- `gemini-2.5-flash`, exam-style quoting: median → `{"answer": {"median": 15.0}}`
  in 3.7s, 2 steps. The unwrap safety net caught gemini's partial wrap.
- `gpt-4o`: also wraps (`{"answer": {...}, "log_url": ""}`) — unwrap handles it.

## What is working
- Full test suite: **59 passed, 1 deselected** (`test_fetch_url_json` — known
  network-flaky, unrelated).
- Agent loop, sandbox (numpy/pandas/requests/bs4), shape enforcement, unwrap,
  per-chat ordering, cross-chat parallelism — all verified by tests + live smoke.

## What is broken or incomplete
- NOT YET DEPLOYED. The VM `tds-p1-bot` still runs the previous code (`ff5b1c2`)
  with `openai/gpt-4o`. Next step is the deploy below.
- The VM `.env` has `LLM_MODEL=openai/gpt-4o` — must be changed to
  `google/gemini-2.5-flash` during deploy.
- `test_fetch_url_json` remains network-dependent and flaky (deselected).
- The OpenRouter key `sk-or-v1-2ac54e0d…` was exposed in an earlier transcript
  and has NOT been rotated. Do it if that transcript is shared.

## Next step — deploy
1. Commit the graft on the bot repo (`github.com/pk1308/tds-p1-telegram-bot`).
2. `gcloud compute ssh tds-p1-bot --zone asia-south1-a`.
3. `sudo -u tdsbot git pull` in the bot dir.
4. `sudo -u tdsbot uv sync` (pulls numpy/requests/bs4).
5. Edit the VM `.env`: `LLM_MODEL=google/gemini-2.5-flash`.
6. `sudo systemctl restart tds-p1-bot`.
7. Send the MOSPI question live; confirm the reply is
   `{"answer": {"state": ...}, "log_url": ...}` and log_url is wget-able.

## Decisions made
- Full graft of the reference engine (fenced-python + FINAL_ANSWER + shape
  enforcement) onto our infra — user-approved.
- Model = `google/gemini-2.5-flash` (the reference's model; fast, cheap,
  follows shape-fidelity). User flagged "the models also" — endorsed switch.
- Sandbox opened (unrestricted imports) — user-approved ("Full graft"); the
  import restriction was the core capability gap.
- `_unwrap_answer` added as a safety net beyond the reference, because both
  gpt-4o and gemini were observed wrapping the answer.
- Reply format = `{"answer": <inner>, "log_url": <URL>}` per `question.md`.

## Files touched (this session)
- `agent.py`, `python_runner.py`, `bot.py`, `config.py`, `.env.example`,
  `README.md`, `CHANGES.md`
- `test_agent_protocol.py` (new), `test_concurrency.py` (new)
- `test_tools.py`, `test_context.py`, `test_cold_start.py`, `test_review_fixes.py`
- removed `test_action_parsing.py`

## Active tasks from TASKS.md
- [ ] TASK-14 · Deploy graft to GCP VM, switch .env to gemini-2.5-flash, smoke-test MOSPI live