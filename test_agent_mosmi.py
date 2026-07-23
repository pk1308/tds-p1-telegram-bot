"""Manual test: run the agent on the sample MOSPI question."""
from __future__ import annotations

import json

import agent
from logger import RunLogger


def main():
    question = 'Which state has the highest maternal mortality rate based on MOSPI data? Reply with ONLY a JSON object like {"state": "<state name>"}'
    logger = RunLogger()
    result = agent.solve(question, logger)
    print("RESULT:", json.dumps(result, ensure_ascii=False, default=str))
    for line in logger._lines:
        print(line.get("event"), ":", (line.get("response") or line.get("observation") or "")[:200])


if __name__ == "__main__":
    main()
