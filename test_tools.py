"""Smoke tests for the agent tools."""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import tools


def test_run_python_adds() -> None:
    out = tools.run_python("a = 2 + 2\nprint(a)")
    assert "4" in out, out


def test_run_python_last_expression() -> None:
    out = tools.run_python("[x * 2 for x in range(3)]")
    assert "[0, 2, 4]" in out or "Result: [0, 2, 4]" in out, out


def test_run_python_import_blocked() -> None:
    out = tools.run_python("import os\nprint(os.getcwd())")
    assert "error" in out.lower() or "No module" in out, out


def test_fetch_url_json() -> None:
    out = tools.fetch_url("https://httpbin.org/json")
    assert "format" in out, out
