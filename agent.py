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
- If a PDF or HTML table is fetched, use Python (pypdf or pandas.read_html) to extract the relevant numbers.
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


ACTION_RE = re.compile(
    r'^\s*Action:\s*(\w+)\((.*)\)\s*$',
    re.IGNORECASE | re.MULTILINE,
)
FINAL_ANSWER_RE = re.compile(
    r'^\s*Final\s*Answer:\s*(.*)$',
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


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


def solve(question: str, logger: RunLogger) -> dict[str, Any]:
    """Run the agent on the final question and return the inner answer dict/value."""
    shape = _extract_requested_shape(question)
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
            response = _chat(messages)
        except Exception as exc:  # noqa: BLE001
            logger.log("llm_error", {"error": f"{type(exc).__name__}: {exc}"})
            return {"error": f"LLM call failed: {exc}"}

        logger.log("llm_response", {"step": step, "response": response})

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
            return {"answer": answer_value}

        action_match = ACTION_RE.search(response)
        if not action_match:
            # Model didn't follow format; nudge it.
            messages.append({"role": "assistant", "content": response})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You must either call exactly one Action or give a Final Answer. "
                        "Do not include explanation outside the Action/Final Answer line."
                    ),
                }
            )
            logger.log("format_nudge", {"step": step})
            continue

        tool_name, raw_args = action_match.group(1), action_match.group(2)
        observation = _call_tool(tool_name, raw_args)
        logger.log("tool_call", {"step": step, "tool": tool_name, "args": raw_args, "observation": observation[:2000]})
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": f"Observation:\n{observation}"})

    logger.log("agent_timeout", {"steps": config.MAX_AGENT_STEPS})
    return {"error": f"Agent did not produce a Final Answer within {config.MAX_AGENT_STEPS} steps"}
