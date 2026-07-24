"""Unit tests for the agent's Action parser."""
from __future__ import annotations

from agent import _find_tool_call


def test_single_line_action_prefix():
    name, args = _find_tool_call('Action: web_search("foo bar")')
    assert name == "web_search"
    assert args == '"foo bar"'


def test_raw_single_line_tool():
    name, args = _find_tool_call('fetch_url("https://example.com")')
    assert name == "fetch_url"
    assert args == '"https://example.com"'


def test_multiline_run_python_with_inner_function_call():
    """Regression: the old parser matched max(...) inside the code as a tool."""
    response = '''run_python("""
data = {"Assam": 110, "Odisha": 153}
max_state = max(data, key=data.get)
max_state
""")'''
    result = _find_tool_call(response)
    assert result is not None
    name, args = result
    assert name == "run_python"
    assert "max(data, key=data.get)" in args


def test_final_answer_not_parsed_as_tool():
    assert _find_tool_call('Final Answer: {"state": "Assam"}') is None
