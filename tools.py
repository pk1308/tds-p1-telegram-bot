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
import shutil
import statistics
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from ddgs import DDGS

import config


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Cap on how many bytes fetch_url will download from one URL, so a hostile or
# gigantic response can't exhaust memory before truncation kicks in.
MAX_RESPONSE_BYTES = 2_000_000

# Only these government hosts may be re-fetched with TLS verification disabled.
# Some Indian govt sites serve broken certs; for everything else a cert failure
# is treated as a real failure (no verify=False retry → no MITM data into the LLM).
GOV_TLS_ALLOWLIST = (
    "mospi.gov.in",
    "censusindia.gov.in",
    "data.gov.in",
    "rbi.org.in",
    "niti.gov.in",
    "indiabudget.gov.in",
    "census.gov.in",
    "gov.in",
    "nic.in",
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


def _is_gov_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == g or host.endswith("." + g) for g in GOV_TLS_ALLOWLIST)


def _stream(url: str, verify: bool) -> tuple[str, str | None, bytes, bool]:
    """Stream `url`, reading at most MAX_RESPONSE_BYTES.

    Returns (content_type, encoding, body_bytes, capped). Streaming with a byte
    cap means a huge URL can't be fully buffered into memory before truncation.
    """
    with httpx.stream(
        "GET",
        url,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=30,
        verify=verify,
    ) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").lower()
        encoding = resp.encoding
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_RESPONSE_BYTES:
                break
        return content_type, encoding, b"".join(chunks), total >= MAX_RESPONSE_BYTES


def fetch_url(url: str, max_len: int = 12000) -> str:
    """Fetch a URL and return its content as text. CSVs are returned as pretty JSON rows.

    Streams with a byte cap (MAX_RESPONSE_BYTES) so a huge/hostile URL can't
    exhaust memory. On a TLS failure the request is retried with verification
    disabled ONLY for known government hosts (some Indian govt sites serve
    broken certs); for any other host a cert failure is a real failure, so we
    never feed an unauthenticated MITM response into the LLM.
    """
    try:
        content_type, encoding, body, capped = _stream(url, verify=True)
    except Exception as exc:  # noqa: BLE001
        if not _is_gov_host(url):
            return f"fetch_url failed: {type(exc).__name__}: {exc}"
        try:
            content_type, encoding, body, capped = _stream(url, verify=False)
        except Exception as exc2:  # noqa: BLE001
            return f"fetch_url failed: {type(exc2).__name__}: {exc2}"

    text = body.decode(encoding or "utf-8", errors="replace")
    cap_note = f"\n...[truncated at source, >{MAX_RESPONSE_BYTES} bytes]" if capped else ""

    if ".pdf" in url.lower() or "pdf" in content_type:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(body))
            pages = [page.extract_text() or "" for page in reader.pages[:10]]
            full_text = "\n".join(pages)
            return _truncate(full_text, max_len)
        except Exception as exc:  # noqa: BLE001
            return f"fetch_url PDF extraction failed: {type(exc).__name__}: {exc}"
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
            data = json.loads(text)
            return json.dumps({"format": "json", "data": data}, indent=2, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            pass
    return _truncate(text, max_len) + cap_note


def run_python(code: str) -> str:
    """Run Python code in a restricted subprocess and return stdout/stderr."""
    code = code.strip()
    if not code:
        return "No code provided."

    runner_path = Path(__file__).with_name("python_runner.py")
    # Each call gets its own workdir so concurrent runs (the grader fires
    # several chats in parallel) can't clobber each other's code file.
    workdir = Path(tempfile.mkdtemp(prefix="tds_p1_sandbox_"))
    code_path = workdir / "agent_code.py"
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
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "web_search": {"fn": web_search, "description": "Search the web for information or datasets."},
    "fetch_url": {"fn": fetch_url, "description": "Download the contents of a URL (CSV, JSON, HTML)."},
    "run_python": {"fn": run_python, "description": "Execute Python code and return printed output or the last expression."},
}
