"""Cold-start resilience tests: deadline nudge, retry-on-timeout, warmup.

All hermetic — LLM and tools are mocked so no network is hit.
"""
import agent
import config
import tools


class _FakeLogger:
    def start(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass

    def finish(self, *a, **k):
        pass


def _always_tool_call(messages):
    """LLM that never finalizes — forces the deadline path."""
    return 'Thought: still searching\nAction: web_search("more")'


def test_deadline_nudge_forces_final_answer(monkeypatch):
    """Near the step cap, a nudge makes the agent emit a Final Answer."""
    monkeypatch.setattr(config, "MAX_AGENT_STEPS", 5)
    monkeypatch.setattr(agent, "DEADLINE_NUDGE_STEPS", 2)

    def chat(messages):
        if any("Stop calling tools" in m.get("content", "") for m in messages if m["role"] == "user"):
            return 'Final Answer: {"state": "Assam"}'
        return 'Thought: searching\nAction: web_search("x")'

    monkeypatch.setattr(agent, "_chat_with_retry", chat)
    result = agent.solve('Q? reply with {"state": "x"}', _FakeLogger())
    assert result == {"answer": {"state": "Assam"}}


def test_deadline_nudge_only_near_cap(monkeypatch):
    """No nudge is injected when the agent finalizes early (warm path)."""
    monkeypatch.setattr(config, "MAX_AGENT_STEPS", 5)
    monkeypatch.setattr(agent, "DEADLINE_NUDGE_STEPS", 2)
    seen_nudge = {"v": False}

    def chat(messages):
        if any("Stop calling tools" in m.get("content", "") for m in messages if m["role"] == "user"):
            seen_nudge["v"] = True
        return 'Final Answer: {"state": "Odisha"}'

    monkeypatch.setattr(agent, "_chat_with_retry", chat)
    result = agent.solve('Q? reply with {"state": "x"}', _FakeLogger())
    assert result == {"answer": {"state": "Odisha"}}
    assert seen_nudge["v"] is False


def test_timeout_retries_once(monkeypatch):
    """solve_with_retry retries once when the first run times out."""
    calls = {"n": 0}

    def fake_solve(question, logger, history=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"error": "Agent did not produce a Final Answer within 15 steps"}
        return {"answer": {"state": "Assam"}}

    monkeypatch.setattr(agent, "solve", fake_solve)
    result = agent.solve_with_retry("Q", _FakeLogger())
    assert calls["n"] == 2
    assert result == {"answer": {"state": "Assam"}}


def test_no_retry_on_non_timeout_error(monkeypatch):
    """A non-timeout error is not retried (e.g. LLM call failed)."""
    calls = {"n": 0}

    def fake_solve(question, logger, history=None):
        calls["n"] += 1
        return {"error": "LLM call failed: boom"}

    monkeypatch.setattr(agent, "solve", fake_solve)
    result = agent.solve_with_retry("Q", _FakeLogger())
    assert calls["n"] == 1
    assert "error" in result


def test_warmup_calls_llm_and_search(monkeypatch):
    calls = {"llm": 0, "search": 0}

    def fake_chat(messages, timeout=None):
        calls["llm"] += 1
        return "ok"

    def fake_search(query, max_results=5):
        calls["search"] += 1
        return "[]"

    monkeypatch.setattr(agent, "_chat", fake_chat)
    monkeypatch.setattr(tools, "web_search", fake_search)
    agent.warmup()
    assert calls["llm"] == 1
    assert calls["search"] == 1


def test_warmup_swallows_errors(monkeypatch):
    """Warmup must never raise — it's best-effort pre-warming."""
    def boom_chat(messages, timeout=None):
        raise RuntimeError("cold")

    def boom_search(query, max_results=5):
        raise RuntimeError("cold")

    monkeypatch.setattr(agent, "_chat", boom_chat)
    monkeypatch.setattr(tools, "web_search", boom_search)
    agent.warmup()  # must not raise