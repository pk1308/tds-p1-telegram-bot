"""ReAct-style data-analysis agent.

Uses any OpenAI-compatible chat endpoint. The agent is driven by prompt parsing,
so it works even with endpoints that do not support native tool calling.

Allowed tool calls (one per response):
  Action: web_search("maternal mortality rate MOSPI state wise")
  Action: fetch_url("https://example.com/data.csv")
  Action: run_python(\"\"\"import json; print(json.dumps({'state':'Assam'}))\"\"\")
  Final Answer: {"state": "Assam"}
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


SYSTEM_PROMPT = '''\
You are a precise data-analysis assistant inside a Telegram bot.

You will receive a plain-text data-analysis question. Some exchanges are multi-turn — only answer the LAST message.

Your job:
1. Understand what data the question needs and what JSON shape the answer must have.
2. Use tools to find, download, and compute the answer.
3. End with a single JSON value that fits the requested shape.

Available tools (call one at a time):
- web_search(query): search DuckDuckGo and return result snippets.
- fetch_url(url): download a URL and return its text/CSV/JSON content.
- run_python(code): execute Python code and return stdout or the last expression. Allowed imports: math, statistics, json, csv, io, re, datetime, collections, itertools, urllib.request, random, hashlib, base64. Network access is allowed only through urllib inside run_python.

Rules:
- Do not guess. Compute or look up the answer.
- For MOSPI / government datasets, prefer fetching the official CSV/PDF/data source. If an official site fails due to SSL/certificate, try a reliable secondary source (Wikipedia, PIB, The Hindu, Statista, data.gov.in, archived version) or retry with HTTP instead of HTTPS.
- Keep Python code self-contained and print the result.
- If a PDF or HTML table is fetched, the tool output already extracts text/tables. If you fetch a binary PDF directly inside run_python, use `pypdf.PdfReader` (PyPDF2 is also available).
- When you are ready, respond ONLY with: Final Answer: <JSON value>
- Do NOT include markdown, explanations, or prose around the Final Answer.
- The Final Answer value will be placed inside {"answer": <value>, "log_url": ...} by the bot, so return only the inner value (e.g. {"state": "Assam"}, [1,2,3], "42", true, etc.).

Example:
Question: Which state has the highest maternal mortality rate based on MOSPI data? Reply with ONLY a JSON object like {"state": "<state name>"}
Thought: I need the latest MOSPI/SRS maternal mortality data. Let me search for it.
Action: web_search("MOSPI SRS maternal mortality rate state wise latest report")

(observation comes back)

Thought: I found a link to the SRS report. Let me fetch the CSV/data table.
Action: fetch_url("https://www.censusindia.gov.in/.../data.csv")

(observation comes back)

Thought: Now I can compute the state with highest MMR using Python.
Action: run_python("""import csv, io; data = [...]; print the state with max MMR""")

(observation comes back)

Final Answer: {"state": "Assam"}
'''


FINAL_ANSWER_RE = re.compile(
    r'^\s*Final\s*Answer:\s*(.*)$',
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
# Models may prefix with "Action:" or call tools directly. We only accept known tools.
TOOL_NAMES = set(tools.TOOL_SPECS)



def _find_tool_call(response: str) -> tuple[str, str] | None:
    """Extract one known tool call from the model response.

    Accepts both `Action: tool(...)` and raw `tool(...)` forms, including
    multiline triple-quoted arguments (e.g. run_python).
    """
    # Final answers are not tool calls.
    if FINAL_ANSWER_RE.search(response):
        return None

    # 1. Single-line calls (with or without the Action: prefix) anywhere in the
    #    response. This also handles Thought/Action patterns safely.
    for name in TOOL_NAMES:
        line_re = re.compile(
            rf'(?:^\s*Action:\s*)?{re.escape(name)}\s*\(([^\n]*)\)\s*$',
            re.IGNORECASE | re.MULTILINE,
        )
        m = line_re.search(response)
        if m:
            return (name, m.group(1))

    # 2. Whole-response multiline call, e.g. run_python("""...""").
    for name in TOOL_NAMES:
        full_re = re.compile(
            rf'^\s*(?:Action:\s*)?{re.escape(name)}\s*\((.*)\)\s*$',
            re.IGNORECASE | re.DOTALL,
        )
        m = full_re.match(response.strip())
        if m:
            return (name, m.group(1))
    return None



def _extract_requested_shape(question: str) -> str | None:
    """Try to pull out the JSON shape the grader wants, e.g. {"state": "<...>"}."""
    # Look for a JSON-like literal in the question.
    candidates = re.findall(r'\{[^{}]*\}', question)
    for c in candidates:
        try:
            json.loads(c)
            return c
        except json.JSONDecodeError:
            continue
    return None


def _call_tool(name: str, raw_args: str) -> str:
    spec = tools.TOOL_SPECS.get(name)
    if spec is None:
        return f"Unknown tool '{name}'. Allowed: {', '.join(tools.TOOL_SPECS)}"

    # Parse a single string argument (supports triple-quoted blocks).
    raw_args = raw_args.strip()
    if (raw_args.startswith('"""') and raw_args.endswith('"""')) or (
        raw_args.startswith("'''") and raw_args.endswith("'''")
    ):
        arg = raw_args[3:-3]
    elif raw_args.startswith('"') and raw_args.endswith('"'):
        arg = raw_args[1:-1]
    elif raw_args.startswith("'") and raw_args.endswith("'"):
        arg = raw_args[1:-1]
    else:
        arg = raw_args
    try:
        return str(spec["fn"](arg))
    except Exception as exc:  # noqa: BLE001
        return f"Tool error: {type(exc).__name__}: {exc}"


def _chat(messages: list[dict[str, str]]) -> str:
    """Call the configured OpenAI-compatible chat endpoint."""
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/pk1308/tds-p1-telegram-bot",
        "X-Title": "TDS P1 Telegram Bot",
    }
    if "aipipe" in config.LLM_BASE_URL.lower():
        # AIPIPE memory: avoid temperature, max_tokens; use a custom UA to dodge Cloudflare 1010.
        payload.pop("temperature", None)
        payload.pop("max_tokens", None)
        headers["User-Agent"] = "Mozilla/5.0 TDS-P1-Bot/1.0"

    resp = httpx.post(
        f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions",
        json=payload,
        headers=headers,
        timeout=config.LLM_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _chat_with_retry(messages: list[dict[str, str]]) -> str:
    """Call _chat with exponential backoff on transient failures."""
    last_error = ""
    for attempt in range(config.LLM_RETRY_ATTEMPTS + 1):
        try:
            return _chat(messages)
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
            original_model = config.LLM_MODEL
            config.LLM_MODEL = config.LLM_FALLBACK_MODEL
            return _chat(messages)
        finally:
            config.LLM_MODEL = original_model

    raise httpx.TransportError(f"LLM unavailable after {config.LLM_RETRY_ATTEMPTS} retries: {last_error}")


def solve(question: str, logger: RunLogger) -> dict[str, Any]:
    """Run the agent on the final question and return the inner answer dict/value."""
    shape = _extract_requested_shape(question)
    run_start = time.monotonic()
    logger.start(question, config.LLM_MODEL)
    format_nudges = 0
    last_observation = ""
    logger.log("agent_start", {"question": question, "requested_shape": shape})

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Question:\n{question}\n\n"
                f"Requested JSON shape (if any): {shape or 'not explicitly specified'}\n\n"
                "Think step by step. Call one tool at a time. End with exactly:\n"
                "Final Answer: <JSON value>"
            ),
        },
    ]

    for step in range(config.MAX_AGENT_STEPS):
        try:
            t0 = time.monotonic()
            response = _chat_with_retry(messages)
            duration_ms = (time.monotonic() - t0) * 1000
        except Exception as exc:  # noqa: BLE001
            logger.log("llm_error", {"error": f"{type(exc).__name__}: {exc}"})
            logger.finish({"error": f"LLM call failed: {exc}"}, step + 1, (time.monotonic() - run_start) * 1000, "llm_error", str(exc))
            return {"error": f"LLM call failed: {exc}"}

        logger.log("llm_response", {"step": step, "response": response, "duration_ms": duration_ms})

        final_match = FINAL_ANSWER_RE.search(response)
        if final_match:
            raw = final_match.group(1).strip()
            # Strip markdown fences if the model added them.
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            try:
                answer_value = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.log("parse_error", {"raw": raw, "error": str(exc)})
                return {"error": f"Final answer is not valid JSON: {exc}", "raw": raw}
            logger.log("agent_done", {"answer": answer_value, "steps": step + 1})
            logger.finish({"answer": answer_value}, step + 1, (time.monotonic() - run_start) * 1000, "success")
            return {"answer": answer_value}

        tool_call = _find_tool_call(response)
        if tool_call is None:
            # Model didn't follow format; nudge it.
            format_nudges += 1
            if format_nudges > 3:
                error_msg = "Agent could not follow tool format after 3 nudges"
                logger.log("agent_format_fail", {"nudges": format_nudges})
                logger.finish({"error": error_msg}, step + 1, (time.monotonic() - run_start) * 1000, "timeout")
                return {"error": error_msg}
            messages.append({"role": "assistant", "content": response})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You must either call exactly one Action or give a Final Answer. "
                        "Valid actions: web_search(...), fetch_url(...), run_python(...). "
                        "Do not include explanation outside the Action/Final Answer line."
                    ),
                }
            )
            logger.log("format_nudge", {"step": step})
            continue

        tool_name, raw_args = tool_call
        observation = _call_tool(tool_name, raw_args)
        last_observation = observation[:500]
        logger.log("tool_call", {"step": step, "tool": tool_name, "args": raw_args, "observation": observation[:2000]})
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": f"Observation:\n{observation}"})

    logger.log("agent_timeout", {"steps": config.MAX_AGENT_STEPS, "last_observation": last_observation})
    error_msg = f"Agent did not produce a Final Answer within {config.MAX_AGENT_STEPS} steps"
    logger.finish({"error": error_msg}, config.MAX_AGENT_STEPS, (time.monotonic() - run_start) * 1000, "timeout")
    return {"error": error_msg}
