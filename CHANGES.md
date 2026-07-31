# CHANGES

## 2026-07-31 — Full graft of reference agent engine
- Removed `test_action_parsing.py`. It tested the old `Action: tool(...)` parser
  (`agent._find_tool_call`), which no longer exists after the graft to the
  fenced-python + `FINAL_ANSWER` protocol. The new protocol parsing is covered
  by `test_agent_protocol.py`. The file was dead (import error broke collection)
  and redundant.
- Removed restricted-BUILTINS isolation in `python_runner.py` (replaced with a
  full-builtins exec exposing pandas/numpy/requests/bs4). Intentional: the
  import restriction was the core capability gap; containment is now temp
  workdir + timeout + non-root service user, matching the reference bot.