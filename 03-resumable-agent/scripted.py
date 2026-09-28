"""A scripted model whose answer depends only on how far the conversation has
progressed, so a fresh process asking at the same point gets the same answer.
Used to crash and restart runs offline.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "01-llm-runtime"))
from llm import STOP_END, STOP_TOOL_USE, AssistantTurn, Completion, ToolCall, check  # noqa: E402


class ScriptedModel:
    def __init__(self, script: list, cost_usd_per_call: float = 0.0):
        self.script = script            # entry i answers the (i+1)-th model call of a run
        self.cost = cost_usd_per_call
        self.calls = 0                  # model calls made to this instance

    def step(self, history, *, system="", tools=(), max_tokens=1024):
        self.calls += 1
        entry = self.script[sum(isinstance(m, AssistantTurn) for m in history)]
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, ToolCall):
            entry = [entry]
        if isinstance(entry, list):
            text, calls, stop = "", tuple(entry), STOP_TOOL_USE
        else:
            text, calls, stop = entry, (), STOP_END
        return check(Completion("scripted", text, None, stop, len(history), 1, 0.0, self.cost, calls))
