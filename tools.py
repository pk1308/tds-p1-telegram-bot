"""Tools for the data-analysis agent.

- web_search: DuckDuckGo lite HTML search.
- fetch_url: download a URL and return text (with basic CSV / HTML detection).
- run_python: execute Python code in a restricted subprocess with a timeout.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import httpx
from ddgs import DDGS

import config


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _truncate(text: str, max_len: int = 8000) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 100] + f"\n...[truncated, total {len(text)} chars]"


def web_search(query: str, max_results: int = 5) -> str:
    """Search DuckDuckGo and return result snippets."""
    try:
        with DDGS() as ddgs:
            raw = ddgs.text(query, max_results=max_results)
        results = [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in raw
        ]
        if not results:
            return "No search results found. Try a more specific query or fetch a known URL directly."
        return json.dumps(results, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"web_search failed: {type(exc).__name__}: {exc}"


def fetch_url(url: str, max_len: int = 12000) -> str:
    """Fetch a URL and return its content as text. CSVs are returned as pretty JSON rows."""
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        # Some government/older sites have certificate issues. Retry once without verification.
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=30,
                verify=False,
            )
            resp.raise_for_status()
        except Exception as exc2:  # noqa: BLE001
            return f"fetch_url failed: {type(exc2).__name__}: {exc2}"

    content_type = resp.headers.get("content-type", "").lower()
    text = resp.text
    if ".csv" in url.lower() or "csv" in content_type:
        try:
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            return json.dumps(
                {"format": "csv", "rows": rows[:200], "total_rows": len(rows)},
                indent=2,
                ensure_ascii=False,
            )
        except Exception:  # noqa: BLE001
            pass
    if ".json" in url.lower() or "json" in content_type:
        try:
            data = resp.json()
            return json.dumps({"format": "json", "data": data}, indent=2, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            pass
    return _truncate(text, max_len)


def run_python(code: str) -> str:
    """Run Python code in a restricted subprocess and return stdout/stderr."""
    code = code.strip()
    if not code:
        return "No code provided."

    runner_path = Path(__file__).with_name("python_runner.py")
    code_path = Path(tempfile.gettempdir()) / "tds_p1_python_code.py"
    code_path.write_text(code, encoding="utf-8")

    try:
        proc = subprocess.run(
            [sys.executable, str(runner_path), str(code_path)],
            capture_output=True,
            text=True,
            timeout=config.PYTHON_TIMEOUT,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return f"Python error (exit {proc.returncode}):\n{err or out}"
        if "__RESULT__" in out:
            parts = out.split("__RESULT__", 1)
            return f"{parts[0].strip()}\nResult: {parts[1].strip()}".strip()
        return out if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Python execution timed out after {config.PYTHON_TIMEOUT}s."
    except Exception as exc:  # noqa: BLE001
        return f"run_python failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "web_search": {"fn": web_search, "description": "Search the web for information or datasets."},
    "fetch_url": {"fn": fetch_url, "description": "Download the contents of a URL (CSV, JSON, HTML)."},
    "run_python": {"fn": run_python, "description": "Execute Python code and return printed output or the last expression."},
}
