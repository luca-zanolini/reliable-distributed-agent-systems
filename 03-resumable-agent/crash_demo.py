"""Crash experiment: kill the agent process mid-action, restart it, count side effects.

Usage: python crash_demo.py        (offline, free: the model is scripted)

Each scenario runs the agent in a child process that is killed (os._exit, no
cleanup) immediately after a side-effecting tool has executed and before its
result is recorded: the window in which a restart cannot know whether the action
happened. The parent then restarts the run and counts the effects.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "02-single-agent"))
sys.path.insert(0, str(HERE.parent / "01-llm-runtime"))
from agent import Agent  # noqa: E402
from durable_agent import DurableAgent  # noqa: E402
from effects import APPEND_NOTE, SEND_MESSAGE  # noqa: E402
from llm import ToolCall  # noqa: E402
from scripted import ScriptedModel  # noqa: E402
from tools import READ_ONLY, Tool, Workspace  # noqa: E402

OBJECTIVE = "Check the test log and alert on-call if anything failed."
LOG = "test fencing::stale_writer_rejected ... FAILED\n"

SCENARIOS = {
    "keyed": (SEND_MESSAGE, {"to": "on-call", "text": "fencing test failing"}),
    "unsafe": (APPEND_NOTE, {"text": "fencing test failing"}),
}


def script(tool_name, args):
    return [ToolCall("c1", "read_file", {"path": "test-output.log"}),
            ToolCall("c2", tool_name, args),
            "Reported the failing fencing test."]


def dying(tool: Tool) -> Tool:
    """The same tool, but the process dies right after its effect happens."""
    def run(*a):
        out = tool.run(*a)
        os._exit(137)
        return out
    return Tool(tool.name, tool.description, tool.args, run, tool.path_args, tool.effect)


def build(runtime, scenario, ws, journal, crash):
    tool, args = SCENARIOS[scenario]
    tools = READ_ONLY + [dying(tool) if crash else tool]
    model = ScriptedModel(script(tool.name, args))
    if runtime == "naive":
        return Agent(model, Workspace(ws), tools)
    return DurableAgent(model, Workspace(ws), tools, journal)


def effects(ws):
    out = Path(ws, "outbox.jsonl")
    notes = Path(ws, "notes.log")
    return (len(out.read_text().splitlines()) if out.exists() else 0) + \
           (len(notes.read_text().splitlines()) if notes.exists() else 0)


def experiment(runtime, scenario):
    with tempfile.TemporaryDirectory() as ws:
        Path(ws, "test-output.log").write_text(LOG)
        journal = Path(ws, ".journal.jsonl")
        # 1. First attempt, in a child process that is killed mid-action.
        child = subprocess.run([sys.executable, __file__, "--child", runtime, scenario, ws, str(journal)])
        # 2. Restart, in this process.
        r = build(runtime, scenario, ws, journal, crash=False).run(OBJECTIVE)
        return child.returncode, effects(ws), r


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        _, _, runtime, scenario, ws, journal = sys.argv
        build(runtime, scenario, ws, journal, crash=True).run(OBJECTIVE)
        sys.exit(0)                         # not reached: the tool kills the process

    print(f"{'runtime':<9} {'action':<14} {'killed':<7} {'effects':<8} {'recovery':<34} outcome")
    for runtime in ("naive", "durable"):
        for scenario in ("keyed", "unsafe"):
            code, n, r = experiment(runtime, scenario)
            name = SCENARIOS[scenario][0].name
            recovery = "; ".join(e.detail.split(": ", 1)[1] for e in r.events
                                 if e.kind in ("recovered", "uncertain")) or "restarted from scratch"
            print(f"{runtime:<9} {name:<14} {'yes' if code == 137 else code:<7} {n:<8} {recovery:<34} {r.status}")
