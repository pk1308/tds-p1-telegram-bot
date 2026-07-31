"""Tests for the grafted agent protocol (fenced-python + FINAL_ANSWER).

These pin the reference bot's proven engine: the model emits fenced ```python
blocks (executed by the sandbox) and finishes with a `FINAL_ANSWER: <json>` line.
The answer is then shape-enforced against the template extracted from the
question so no extra keys leak into the exact-matched graded value.
"""
from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:token")
os.environ.setdefault("LLM_API_KEY", "dummy-key")
os.environ.setdefault("GCS_LOG_BUCKET", "dummy-bucket")

import agent


# --- FINAL_ANSWER extraction -------------------------------------------------

def test_extract_final_parses_json():
    ans, raw = agent._extract_final('blah\nFINAL_ANSWER: {"state": "Assam"}\ntail')
    assert ans == {"state": "Assam"}
    assert raw == '{"state": "Assam"}'


def test_extract_final_accepts_legacy_final_answer_form():
    """We also accept `Final Answer:` (the old form) so existing flows work."""
    ans, raw = agent._extract_final('Final Answer: {"median": 15}')
    assert ans == {"median": 15}


def test_extract_final_handles_scalar_and_list():
    assert agent._extract_final("FINAL_ANSWER: 42")[0] == 42
    assert agent._extract_final("FINAL_ANSWER: [3, 1, 4]")[0] == [3, 1, 4]
    assert agent._extract_final('FINAL_ANSWER: "2023-04"')[0] == "2023-04"


def test_extract_final_absent_returns_none_none():
    assert agent._extract_final("just some prose, no answer") == (None, None)


def test_extract_final_unparseable_returns_none_raw():
    """A present but unparseable FINAL_ANSWER yields (None, raw) so solve can
    take the parse-error branch (close the run, return an error)."""
    ans, raw = agent._extract_final("FINAL_ANSWER: {bad json")
    assert ans is None
    assert raw is not None


# --- template extraction -----------------------------------------------------

def test_extract_template_from_answer_wrapper():
    """The exam format embeds the template inside {"answer": <template>, "log_url": ...}.
    We must return the INNER template, not the wrapper."""
    q = ('Which state has the highest maternal mortality rate? Reply with ONLY '
         'this JSON object: {"answer": {"state": "<state name>"}, "log_url": "<URL>"}')
    assert agent._extract_template(q) == {"state": "<state name>"}


def test_extract_template_none_when_no_answer_key():
    """A bare template like {"state": "x"} has no "answer" key -> no filtering
    (the agent's answer is already the right shape)."""
    assert agent._extract_template('Q? reply {"state": "x"}') is None


def test_extract_template_none_when_unparseable_placeholder():
    """{"median": <number>} is not valid JSON -> None (no guessed template)."""
    assert agent._extract_template('median? reply {"median": <number>}') is None


# --- shape enforcement -------------------------------------------------------

def test_enforce_shape_strips_extra_keys():
    template = {"state": "<state name>"}
    answer = {"state": "Assam", "reduction": 58, "source": "SRS"}
    assert agent._enforce_shape(answer, template) == {"state": "Assam"}


def test_enforce_shape_recursive_nested():
    template = {"answer": {"state": "<x>"}, "log_url": "<u>"}
    answer = {"answer": {"state": "Assam", "extra": 1}, "log_url": "http://x", "junk": True}
    out = agent._enforce_shape(answer, template)
    assert out == {"answer": {"state": "Assam"}, "log_url": "http://x"}


def test_enforce_shape_list_of_dicts():
    template = [{"k": "<v>"}]
    answer = [{"k": "a", "x": 1}, {"k": "b", "x": 2}]
    out = agent._enforce_shape(answer, template)
    assert out == [{"k": "a"}, {"k": "b"}]


def test_enforce_shape_none_template_passes_through():
    assert agent._enforce_shape({"state": "Assam"}, None) == {"state": "Assam"}


def test_unwrap_answer_strips_mistaken_wrapper():
    """If the model emits the full {"answer": ..., "log_url": ...} wrapper but the
    template is the inner shape, unwrap to the inner value before enforcing."""
    template = {"state": "<state name>"}
    wrapped = {"answer": {"state": "Assam", "x": 1}, "log_url": "http://x"}
    assert agent._unwrap_answer(wrapped, template) == {"state": "Assam", "x": 1}


def test_unwrap_answer_noop_when_no_answer_key():
    """A correctly unwrapped answer (no "answer" key) is left alone."""
    assert agent._unwrap_answer({"state": "Assam"}, {"state": "<x>"}) == {"state": "Assam"}


def test_unwrap_answer_noop_when_template_has_answer_key():
    """If the template itself has an "answer" key, never unwrap."""
    template = {"answer": "<a>"}
    assert agent._unwrap_answer({"answer": "v"}, template) == {"answer": "v"}


def test_solve_unwraps_then_enforces_on_wrapped_final_answer(monkeypatch):
    """End-to-end: a wrapped FINAL_ANSWER is unwrapped and shape-enforced."""
    monkeypatch.setattr(
        agent, "_chat_with_retry",
        lambda msgs: 'FINAL_ANSWER: {"answer": {"state": "Assam", "reduction": 58}, "log_url": "http://x"}',
    )

    class _Log:
        def start(self, *a, **k): pass
        def log(self, *a, **k): pass
        def finish(self, *a, **k): pass

    q = ('Which state? Reply with ONLY this JSON object: '
         '{"answer": {"state": "<state name>"}, "log_url": "<URL>"}')
    result = agent.solve(q, _Log())
    assert result == {"answer": {"state": "Assam"}}


# --- fenced-python block extraction -----------------------------------------

def test_python_block_regex_captures_code():
    resp = 'Thought: compute\n```python\nimport numpy as np\nprint(np.array([1,2]).sum())\n```\ndone'
    blocks = agent._PYTHON_BLOCK.findall(resp)
    assert len(blocks) == 1
    assert "numpy" in blocks[0]


def test_solve_runs_python_block_then_final_answer(monkeypatch):
    """The loop executes a fenced python block (mocked) and then accepts the
    FINAL_ANSWER from the next response."""
    import tools

    seq = [
        '```python\nprint(2+2)\n```',
        'FINAL_ANSWER: {"state": "Assam"}',
    ]
    calls = {"chat": 0, "py": 0}

    def fake_chat_with_retry(messages):
        i = calls["chat"]
        calls["chat"] += 1
        return seq[i]

    def fake_run_python(code):
        calls["py"] += 1
        return "4"

    monkeypatch.setattr(agent, "_chat_with_retry", fake_chat_with_retry)
    monkeypatch.setattr(tools, "run_python", fake_run_python)

    class _Log:
        def start(self, *a, **k): pass
        def log(self, *a, **k): pass
        def finish(self, *a, **k): pass

    result = agent.solve('Q? reply {"state": "x"}', _Log())
    assert result == {"answer": {"state": "Assam"}}
    assert calls["py"] == 1
    assert calls["chat"] == 2


def test_solve_enforces_shape_on_final_answer(monkeypatch):
    """Extra keys in the FINAL_ANSWER are stripped against the exam template."""
    monkeypatch.setattr(agent, "_chat_with_retry",
                        lambda msgs: 'FINAL_ANSWER: {"state": "Assam", "reduction": 58}')

    class _Log:
        def start(self, *a, **k): pass
        def log(self, *a, **k): pass
        def finish(self, *a, **k): pass

    q = ('Which state? Reply with ONLY this JSON object: '
         '{"answer": {"state": "<state name>"}, "log_url": "<URL>"}')
    result = agent.solve(q, _Log())
    assert result == {"answer": {"state": "Assam"}}