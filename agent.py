"""The data-analyst agent.

A text-based ReAct loop over an OpenAI-compatible chat endpoint (OpenRouter by
default). The model computes by emitting fenced ```python blocks (executed by
tools.run_python) and finishes by emitting a line `FINAL_ANSWER: <json>`. We
deliberately avoid native function-calling so the same loop works across every
OpenRouter model (GPT, Gemini, Claude, ...).

This is a graft of the proven reference bot engine onto our own infrastructure
(per-run GCS logging, per-chat context reset, warmup, retry/fallback, deadline
nudge, and the {"answer": ..., "log_url": ...} contract wrapper in bot.py).

The agent returns only the inner *answer* value (the exact JSON shape the
question asked for), shape-enforced against the template extracted from the
question so no extra keys leak into the exact-matched graded value. The bot
wraps it as {"answer": ..., "log_url": ...}.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

import config
import tools
from logger import RunLogger


SYSTEM_PROMPT = """You are an expert data analyst. You receive a data-analysis question and must
work out the answer, then reply with ONLY the answer in the exact JSON shape the
question requests.

HOW TO WORK:
- To compute anything, output a fenced code block:
  ```python
  # your code
  ```
  You will see its stdout and stderr. You may run code several times.
- You have pandas (as pd), numpy (as np), requests, bs4, pypdf, and the stdlib.
  web_search(query) and fetch_url(url) are also available as functions.
- If the question points at a public dataset or URL, fetch it with requests (or
  fetch_url) and parse it. If the data is inline in the message, parse it directly.
- Numbers must be computed, not guessed. Round only if the question asks.

HOW TO FINISH:
- When you have the answer, output exactly one line:
  FINAL_ANSWER: <a single JSON value matching the requested shape>
- That JSON value is the contents of the "answer" field the user asked for.
  Do NOT include the keys "answer" or "log_url" yourself, and do NOT wrap it.
  Examples of correct FINAL_ANSWER lines:
    FINAL_ANSWER: {"state": "Assam"}
    FINAL_ANSWER: 42
    FINAL_ANSWER: [3, 1, 4]
    FINAL_ANSWER: "2023-04"
- Output nothing after the FINAL_ANSWER line. No explanation, no prose.

SHAPE FIDELITY (critical — answers are exact-matched):
- Use EXACTLY the keys the question's JSON template shows — no more, no fewer.
  Copy the key names and structure from the template the message provides.
- NEVER add extra keys, even if informative. If the template is
  {"state": "<state name>"} output {"state": "Assam"} — NOT
  {"state": "Assam", "reduction": 58}. Extra keys fail grading.
- Match value types: a string placeholder "<state name>" -> a string;
  a number placeholder <number> -> a number; a list -> a list. Round numbers
  only if the question asks for rounding.
- If no template is given, return the minimal JSON that answers the question.

Some exchanges are multi-turn: answer ONLY the current (last) message; earlier
messages are provided as context only.

Be rigorous. Verify the shape matches what the question asked before finishing."""


# Accept the reference's `FINAL_ANSWER:` and the legacy `Final Answer:` form.
_FINAL = re.compile(r"FINAL[\s_]?ANSWER:\s*(.+)", re.IGNORECASE | re.DOTALL)
# Fenced python blocks the model emits to compute.
_PYTHON_BLOCK = re.compile(r"```python\s*\n(.*?)```", re.S)

# Inject a finalize-now nudge this many steps before the cap so the agent
# always emits a best-guess FINAL_ANSWER instead of silently exhausting steps.
DEADLINE_NUDGE_STEPS = 3
# After this many format nudges with no python block and no FINAL_ANSWER, give up.
_FORMAT_NUDGE_CAP = 3


def _extract_final(text: str) -> tuple[Any, str | None]:
    """Return (answer_value, raw_json_str) if a FINAL_ANSWER is present.

    (None, None) means no FINAL_ANSWER line was found. (None, raw) means one was
    found but could not be parsed — the caller treats that as a parse error.
    """
    m = _FINAL.search(text)
    if not m:
        return None, None
    raw = m.group(1).strip().splitlines()[0].strip().rstrip("`")
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        # try to grab the first balanced JSON value on the line
        for cand in re.findall(
            r"(\{.*\}|\[.*\]|true|false|null|-?\d+(?:\.\d+)?|\".*\")", raw, re.S
        ):
            try:
                return json.loads(cand), cand
            except json.JSONDecodeError:
                continue
        return None, raw


def _extract_template(question: str) -> Any:
    """Find the JSON answer template the question asks for.

    The exam format is {"answer": <template>, "log_url": "..."}. We scan for
    balanced {...} substrings and return <template> from the first object that
    has an "answer" key. Returns None if no such object is found (then we leave
    the answer untouched — never filter on a guessed template).
    """
    candidates: list[str] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(question):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(question[start : i + 1])
                start = None
    for c in candidates:
        try:
            o = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and "answer" in o:
            return o["answer"]
    return None


def _enforce_shape(answer: Any, template: Any) -> Any:
    """Filter `answer` to the keys/structure of `template` (best-effort, recursive).

    Guarantees no extra keys leak into the graded answer (the grader
    exact-matches). If template is None, returns answer unchanged.
    """
    if template is None or answer is None:
        return answer
    if isinstance(template, dict) and isinstance(answer, dict):
        out = {}
        for k, tv in template.items():
            if k in answer:
                out[k] = _enforce_shape(answer[k], tv)
        return out
    if isinstance(template, list) and isinstance(answer, list):
        if template and isinstance(template[0], dict):
            return [_enforce_shape(a, template[0]) for a in answer]
        return answer
    return answer


def _unwrap_answer(answer: Any, template: Any) -> Any:
    """If the model mistakenly emitted the full {"answer": ..., "log_url": ...}
    wrapper instead of just the inner value, unwrap it before shape enforcement.

    Only unwraps when the extracted template does NOT itself have an "answer" key
    (so a question that genuinely asks for an "answer" key is left alone). This is
    a safety net: the prompt tells the model not to wrap, but some models (e.g.
    gpt-4o) do it anyway, and without unwrapping _enforce_shape would produce {}.
    """
    if (
        isinstance(answer, dict)
        and "answer" in answer
        and not (isinstance(template, dict) and "answer" in template)
    ):
        inner = answer.get("answer")
        if inner is not None:
            return inner
    return answer


def _chat(messages: list[dict[str, str]], timeout: float | None = None, model: str | None = None) -> str:
    """Call the configured OpenAI-compatible chat endpoint.

    ``model`` defaults to ``config.LLM_MODEL``. Passing it explicitly keeps the
    fallback path from mutating the global config, which other concurrent
    requests read — see _chat_with_retry.
    """
    use_model = model or config.LLM_MODEL
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 3000,
    }
    # Reasoning models benefit from a low effort hint (cheaper, faster).
    if any(k in use_model for k in ("gpt-5", "gemini", "o1", "o3")):
        payload["reasoning"] = {"effort": "low"}
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/pk1308/tds-p1-telegram-bot",
        "X-Title": "TDS P1 Telegram Bot",
    }
    if "aipipe" in config.LLM_BASE_URL.lower():
        # AIPIPE memory: avoid temperature, max_tokens, reasoning; use a custom
        # UA to dodge Cloudflare 1010.
        payload.pop("temperature", None)
        payload.pop("max_tokens", None)
        payload.pop("reasoning", None)
        headers["User-Agent"] = "Mozilla/5.0 TDS-P1-Bot/1.0"

    resp = httpx.post(
        f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions",
        json=payload,
        headers=headers,
        timeout=timeout if timeout is not None else config.LLM_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _chat_with_retry(messages: list[dict[str, str]]) -> str:
    """Call _chat with exponential backoff on transient failures.

    The fallback model is passed as a local argument to _chat — it is NOT
    assigned to config.LLM_MODEL. Telegram handlers run concurrently, so a
    global mutation would leak the fallback model into other requests.
    """
    primary = config.LLM_MODEL
    last_error = ""
    for attempt in range(config.LLM_RETRY_ATTEMPTS + 1):
        try:
            return _chat(messages, model=primary)
        except httpx.TimeoutException as exc:
            last_error = f"Timeout: {exc}"
        except httpx.ConnectError as exc:
            last_error = f"ConnectError: {exc}"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            last_error = f"HTTP {exc.response.status_code}: {exc}"

        if attempt < config.LLM_RETRY_ATTEMPTS:
            wait = min(config.LLM_RETRY_BACKOFF_BASE ** attempt, 8.0)
            time.sleep(wait)

    if config.LLM_FALLBACK_MODEL:
        try:
            return _chat(messages, model=config.LLM_FALLBACK_MODEL)
        except Exception as exc:  # noqa: BLE001
            last_error = f"fallback failed: {type(exc).__name__}: {exc}"

    raise httpx.TransportError(
        f"LLM unavailable after {config.LLM_RETRY_ATTEMPTS} retries: {last_error}"
    )


def solve(question: str, logger: RunLogger, history: list[str] | None = None) -> dict[str, Any]:
    """Run the agent on the current question and return the inner answer.

    Returns ``{"answer": <value>}`` on success or ``{"error": ...}`` on failure.

    ``question`` is the current (last) user message to answer. ``history`` is the
    list of prior user messages in the same question exchange (multi-turn context
    only); prior messages from *other* questions never reach here because the bot
    resets per-chat context between questions.
    """
    template = _extract_template(question)
    run_start = time.monotonic()
    logger.start(question, config.LLM_MODEL)
    logger.log(
        "agent_start",
        {"question": question, "requested_shape": template, "history_turns": len(history or [])},
    )

    context_block = ""
    if history:
        context_block = (
            "Earlier messages in this conversation (for context only):\n"
            + "\n".join(f"- {m}" for m in history)
            + "\n\n"
        )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": context_block + "Current message (answer THIS one):\n" + question,
        },
    ]

    format_nudges = 0
    for step in range(config.MAX_AGENT_STEPS):
        # Near the step cap, push the agent to commit its best answer now.
        if step >= config.MAX_AGENT_STEPS - DEADLINE_NUDGE_STEPS:
            messages.append({
                "role": "user",
                "content": (
                    f"You have {config.MAX_AGENT_STEPS - step} step(s) left. "
                    "Stop calling tools and respond NOW with ONLY: "
                    "FINAL_ANSWER: <best JSON value you can produce from what you have>."
                ),
            })

        try:
            t0 = time.monotonic()
            response = _chat_with_retry(messages)
            duration_ms = (time.monotonic() - t0) * 1000
        except Exception as exc:  # noqa: BLE001
            logger.log("llm_error", {"error": f"{type(exc).__name__}: {exc}"})
            logger.finish(
                {"error": f"LLM call failed: {exc}"}, step + 1,
                (time.monotonic() - run_start) * 1000, "llm_error", str(exc),
            )
            return {"error": f"LLM call failed: {exc}"}

        logger.log("llm_response", {"step": step, "response": response, "duration_ms": duration_ms})

        ans, raw = _extract_final(response)
        if raw is not None:
            # A FINAL_ANSWER line was present.
            if ans is not None:
                ans = _unwrap_answer(ans, template)
                answer = _enforce_shape(ans, template)
                logger.log("agent_done", {"answer": answer, "steps": step + 1})
                logger.finish(
                    {"answer": answer}, step + 1,
                    (time.monotonic() - run_start) * 1000, "success",
                )
                return {"answer": answer}
            # Present but unparseable — close the run, surface a parse error.
            logger.log("parse_error", {"raw": raw, "error": "invalid JSON"})
            logger.finish(
                {"error": f"Final answer is not valid JSON: {raw}", "raw": raw},
                step + 1, (time.monotonic() - run_start) * 1000, "parse_error", "invalid JSON",
            )
            return {"error": f"Final answer is not valid JSON: {raw}", "raw": raw}

        blocks = _PYTHON_BLOCK.findall(response)
        if blocks:
            observation = tools.run_python(blocks[-1])
            logger.log(
                "tool_call",
                {"step": step, "tool": "run_python", "code": blocks[-1][:2000],
                 "observation": observation[:2000]},
            )
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": (
                    f"Code output:\n--- stdout/stderr ---\n{observation}\n"
                    "Continue. If you now have the answer, output "
                    "FINAL_ANSWER: <json>. Otherwise output another python block."
                ),
            })
            continue

        # No FINAL_ANSWER and no python block: maybe it blurted JSON directly.
        stripped = response.strip()
        try:
            ans = json.loads(stripped)
        except json.JSONDecodeError:
            ans = None
        if ans is not None:
            ans = _unwrap_answer(ans, template)
            answer = _enforce_shape(ans, template)
            logger.log("agent_done", {"answer": answer, "steps": step + 1, "via": "direct_json"})
            logger.finish(
                {"answer": answer}, step + 1,
                (time.monotonic() - run_start) * 1000, "success",
            )
            return {"answer": answer}

        # Still no progress: nudge the format, or give up after repeated failure.
        format_nudges += 1
        if format_nudges > _FORMAT_NUDGE_CAP:
            error_msg = "Agent could not follow output format after 3 nudges"
            logger.log("agent_format_fail", {"nudges": format_nudges})
            logger.finish(
                {"error": error_msg}, step + 1,
                (time.monotonic() - run_start) * 1000, "timeout",
            )
            return {"error": error_msg}
        messages.append({"role": "assistant", "content": response})
        messages.append({
            "role": "user",
            "content": (
                "You did not output a ```python block or a FINAL_ANSWER line. "
                "Either emit a ```python block to compute the answer, or finish "
                "with FINAL_ANSWER: <json value in the requested shape>."
            ),
        })
        logger.log("format_nudge", {"step": step})

    error_msg = f"Agent did not produce a Final Answer within {config.MAX_AGENT_STEPS} steps"
    logger.finish(
        {"error": error_msg}, config.MAX_AGENT_STEPS,
        (time.monotonic() - run_start) * 1000, "timeout",
    )
    return {"error": error_msg}


def warmup() -> None:
    """Pre-warm the LLM endpoint, DNS, and tool path on a cold start.

    Best-effort: any failure is swallowed so a cold endpoint can't block bot
    startup. The first real message then behaves like a warm one.
    """
    try:
        _chat(
            [
                {"role": "system", "content": "Reply with the single word: ok"},
                {"role": "user", "content": "ok"},
            ],
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        tools.web_search("test")
    except Exception:  # noqa: BLE001
        pass


def solve_with_retry(question: str, logger: RunLogger, history: list[str] | None = None) -> dict[str, Any]:
    """Run the agent, retrying once on a step-exhaustion timeout.

    Cold-start timeouts are usually self-curing: by the second attempt the LLM
    endpoint, DNS, and subprocess are warm. Non-timeout errors are not retried
    (e.g. an LLM call failure has its own retry in _chat_with_retry).
    """
    result = solve(question, logger, history=history)
    if "error" in result and "did not produce a Final Answer" in result["error"]:
        logger.log("retry_on_timeout", {"first_error": result["error"]})
        result = solve(question, logger, history=history)
    return result