"""Regression tests for the code-review fixes (review on 2026-07-31).

Covers:
  #1 High   — fallback model must not mutate config.LLM_MODEL (agent.py)
  #2 Medium — GCS upload retries instead of fabricating a dead log_url (logger/bot)
  #3 Medium — JSON parse failure still emits run_finish (agent.py)
  #4 Medium — conversation context is concurrency-safe (bot.py)
  #5 Medium — verify=False retry only for gov hosts (tools.py)
  #6 Low    — fetched response is byte-capped while streaming (tools.py)
"""
from __future__ import annotations

import os
import threading

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import httpx

import agent
import bot
import config
import logger
import tools


class _CapLog:
    """Captures every event including run_finish."""

    def __init__(self):
        self.events = []

    def start(self, *a, **k):
        self.events.append(("run_start", a, k))

    def log(self, event, data):
        self.events.append((event, data))

    def finish(self, *a, **k):
        self.events.append(("run_finish", a, k))


# --- #1: fallback model does not mutate global config ------------------------

def test_fallback_does_not_mutate_global_model(monkeypatch):
    """Using the fallback model must not touch config.LLM_MODEL — another
    concurrent request reads it and would log/serve the wrong model."""
    monkeypatch.setattr(config, "LLM_MODEL", "primary-model")
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", "fallback-model")
    monkeypatch.setattr(config, "LLM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(agent.time, "sleep", lambda _s: None)

    used = []

    def fake_chat(messages, timeout=None, model=None):
        used.append(model)
        if (model or config.LLM_MODEL) == "primary-model":
            raise httpx.HTTPStatusError(
                "500",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(500),
            )
        return "ok"

    monkeypatch.setattr(agent, "_chat", fake_chat)
    agent._chat_with_retry([{"role": "user", "content": "hi"}])

    assert config.LLM_MODEL == "primary-model", "global model was mutated by fallback"
    assert "fallback-model" in used, "fallback model was not actually used"


def test_no_fallback_keeps_model(monkeypatch):
    """Without a fallback, a 5xx re-raises and config.LLM_MODEL is untouched."""
    monkeypatch.setattr(config, "LLM_MODEL", "primary-model")
    monkeypatch.setattr(config, "LLM_FALLBACK_MODEL", "")
    monkeypatch.setattr(config, "LLM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(agent.time, "sleep", lambda _s: None)

    def fake_chat(messages, timeout=None, model=None):
        raise httpx.HTTPStatusError(
            "500", request=httpx.Request("POST", "http://x"), response=httpx.Response(500)
        )

    monkeypatch.setattr(agent, "_chat", fake_chat)
    try:
        agent._chat_with_retry([{"role": "user", "content": "hi"}])
    except Exception:
        pass
    assert config.LLM_MODEL == "primary-model"


# --- #3: parse failure emits run_finish --------------------------------------

def test_parse_failure_calls_finish(monkeypatch):
    """An invalid Final Answer JSON must still close the run with run_finish."""
    monkeypatch.setattr(agent, "_chat_with_retry", lambda msgs: "Final Answer: {bad json")
    lg = _CapLog()
    result = agent.solve('Q? reply {"state": "x"}', lg)
    assert "error" in result
    assert any(isinstance(e, tuple) and e and e[0] == "run_finish" for e in lg.events), (
        f"run_finish missing; events={lg.events}"
    )


# --- #4: conversation context is concurrency-safe ----------------------------

def test_store_message_concurrent_no_loss():
    """Concurrent appends must all land — none lost to a read-modify-write race."""
    bot._conversation_context.clear()
    bot._last_seen.clear()
    n = 8  # under _MAX_CONTEXT so nothing is evicted

    def worker(i):
        bot._store_message(123, f"m{i}", now=float(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(bot._conversation_context[123]) == n, bot._conversation_context[123]


# --- #2: GCS upload retries instead of fabricating a dead log_url -----------

def test_finalize_retries_on_transient_failure(monkeypatch):
    """A transient GCS failure must be retried, not turned into a dead URL."""
    lg = logger.RunLogger()
    lg.log("hello", {})
    monkeypatch.setattr(logger.time, "sleep", lambda _s: None)

    attempts = {"n": 0}

    class FakeBlob:
        def upload_from_file(self, f, content_type=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("transient")

    class FakeBucket:
        def blob(self, name):
            return FakeBlob()

    class FakeClient:
        def bucket(self, name):
            return FakeBucket()

    lg._client = FakeClient()
    url = lg.finalize()
    assert attempts["n"] == 3, f"expected 3 attempts, got {attempts['n']}"
    assert url.endswith(lg.object_name), url


def test_finalize_raises_after_persistent_failure(monkeypatch):
    """If every retry fails, finalize raises — the bot must not fabricate a
    reachable-looking URL that would fail the grader's check."""
    lg = logger.RunLogger()
    lg.log("hello", {})
    monkeypatch.setattr(logger.time, "sleep", lambda _s: None)

    class FakeBlob:
        def upload_from_file(self, f, content_type=None):
            raise OSError("down")

    class FakeBucket:
        def blob(self, name):
            return FakeBlob()

    class FakeClient:
        def bucket(self, name):
            return FakeBucket()

    lg._client = FakeClient()
    try:
        lg.finalize()
        assert False, "finalize should have raised"
    except OSError:
        pass


# --- #5: verify=False retry only for gov hosts --------------------------------

def _install_stream(monkeypatch, *, fail_verify, body=b"ok", content_type="text/html",
                    encoding="utf-8", chunk=8192):
    """Replace tools.httpx.stream with a fake that records the `verify` flag of
    each call and optionally raises on the verify=True call."""
    calls = []

    class FakeResp:
        def __init__(self):
            self.headers = {"content-type": content_type}
            self.encoding = encoding

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            for i in range(0, len(body), chunk):
                yield body[i:i + chunk]

    class FakeCtx:
        def __init__(self, method, url, **k):
            self.verify = k.get("verify", True)
            calls.append(self.verify)

        def __enter__(self):
            if fail_verify and self.verify:
                raise httpx.ConnectError("cert")
            return FakeResp()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tools.httpx, "stream", lambda *a, **k: FakeCtx(*a, **k))
    return calls


def test_verify_false_not_used_for_arbitrary_host(monkeypatch):
    """A cert failure on a non-gov host must NOT retry with verify=False
    (would accept a MITM response and feed it to the LLM)."""
    calls = _install_stream(monkeypatch, fail_verify=True)
    tools.fetch_url("https://example.com/data.csv")
    assert calls == [True], f"verify=False used for non-gov host: {calls}"


def test_verify_false_used_for_gov_host(monkeypatch):
    """A cert failure on a gov host still retries with verify=False."""
    calls = _install_stream(monkeypatch, fail_verify=True)
    tools.fetch_url("https://mospi.gov.in/page.html")
    assert calls == [True, False], calls


# --- #6: fetched response is byte-capped while streaming ---------------------

def test_fetch_url_caps_huge_response(monkeypatch):
    """A hostile/huge URL must not be fully buffered — download stops at the cap."""
    huge = b"x" * (tools.MAX_RESPONSE_BYTES + 500_000)
    consumed = {"n": 0}

    class FakeResp:
        headers = {"content-type": "text/plain"}
        encoding = "utf-8"

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            for i in range(0, len(huge), 8192):
                chunk = huge[i:i + 8192]
                consumed["n"] += len(chunk)
                yield chunk

    class FakeCtx:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return FakeResp()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tools.httpx, "stream", lambda *a, **k: FakeCtx(*a, **k))
    out = tools.fetch_url("https://example.com/big.txt")

    assert consumed["n"] <= tools.MAX_RESPONSE_BYTES + 8192, "downloaded past the cap"
    assert consumed["n"] < len(huge), "full body was buffered despite the cap"
    assert len(out) <= 12000 + 100, len(out)  # truncated for the LLM