"""Smoke tests for the agent tools."""
from __future__ import annotations

import os
import threading

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


def test_run_python_allows_data_libs() -> None:
    """The sandbox exposes the data-analysis stack (numpy, pandas, requests,
    bs4) so the agent can do real data work in one fenced block — the core
    capability gap that the graft closes."""
    out = tools.run_python(
        "import numpy as np, pandas as pd, requests, bs4\n"
        "print('libs ok', int(np.array([1, 2]).sum()))"
    )
    assert "libs ok" in out, out
    assert "3" in out, out


def test_run_python_concurrent_no_clobber() -> None:
    """Concurrent run_python calls must not share one code file.

    The old implementation wrote every run to the same /tmp path, so two
    concurrent calls clobbered each other's file mid-execution and returned
    the wrong output. Each call must get its own isolated workdir.
    """
    n = 8
    markers = [f"MARK_{i:02d}" for i in range(n)]
    results: dict[int, str] = {}
    barrier = threading.Barrier(n)

    def run_one(i: int) -> None:
        barrier.wait()  # release all threads at once so writes burst together
        code = f"print('{markers[i]}')"
        results[i] = tools.run_python(code)

    threads = [threading.Thread(target=run_one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(n):
        assert markers[i] in results[i], (
            f"run {i} was clobbered (got {results[i]!r}, expected {markers[i]!r})"
        )


def test_fetch_url_json() -> None:
    out = tools.fetch_url("https://httpbin.org/json")
    assert "format" in out, out
