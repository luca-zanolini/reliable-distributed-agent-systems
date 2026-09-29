"""A resumable agent: the Stage 2 loop with a write-ahead journal.

All run state is a fold over the journal. The in-memory state is a cache that a
restart rebuilds by replaying the records; nothing else survives a crash.

Record types, in the order a step produces them:

  run_started   {run_id, objective}
  model_turn    {step, text, calls, cost}     after the model answers, before any tool
  tool_intent   {call_id, repeat_key}         after authorization, BEFORE execution
  tool_result   {call_id, content, is_error}  after execution
  model_failed  {cost}                        an unusable, billed answer
  run_finished  {status, answer}

Recovery rules, applied to the last model turn's unanswered calls on restart:

  no intent recorded   -> never started: authorize and run normally
  intent, no result    -> may or may not have run. By the tool's effect class:
                          read / idempotent -> re-run
                          keyed             -> re-run with the same idempotency key
                          unsafe            -> do NOT re-run; report "outcome unknown"
  result recorded      -> done: reuse the recorded result, never re-run

Model turns are never re-requested once recorded: the recorded output is
authoritative (the model is nondeterministic and every call is billed).
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "02-single-agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "01-llm-runtime"))
from agent import Agent, Budget, Event, RunResult  # noqa: E402
from journal import Journal  # noqa: E402
from llm import (  # noqa: E402
    AssistantTurn, LLMError, OutputError, RequestError, ToolCall, ToolResult,
    ToolResults, UserText,
)


@dataclass
class RunState:
    run_id: str = ""
    objective: str = ""
    history: list = field(default_factory=list)
    spent: float = 0.0
    steps: int = 0
    seen: dict = field(default_factory=dict)       # repeat counter (check 4)
    pending: list = field(default_factory=list)    # calls of the last turn with no result yet
    buffered: list = field(default_factory=list)   # results collected for the last turn
    intents: set = field(default_factory=set)      # call ids whose execution may have begun
    finished: RunResult | None = None


def apply(st: RunState, r: dict) -> None:
    """The state transition function. Used identically live and on replay."""
    t = r["type"]
    if t == "run_started":
        st.run_id, st.objective = r["run_id"], r["objective"]
        st.history = [UserText(r["objective"])]
    elif t == "model_turn":
        calls = tuple(ToolCall(c["id"], c["name"], c["arguments"]) for c in r["calls"])
        st.steps = r["step"]
        st.spent += r["cost"]
        st.history.append(AssistantTurn(r["text"], calls))
        st.pending, st.buffered = list(calls), []
    elif t == "tool_intent":
        st.intents.add(r["call_id"])
        st.seen[r["repeat_key"]] = st.seen.get(r["repeat_key"], 0) + 1
    elif t == "tool_result":
        st.buffered.append(ToolResult(r["call_id"], r["content"], r["is_error"]))
        st.pending = [c for c in st.pending if c.id != r["call_id"]]
        if not st.pending:
            st.history.append(ToolResults(tuple(st.buffered)))
    elif t == "model_failed":
        st.spent += r["cost"]
    elif t == "run_finished":
        st.finished = RunResult(r["status"], r["answer"], st.steps, st.spent)


class DurableAgent(Agent):
    def __init__(self, provider, workspace, tools, journal_path, budget: Budget = Budget(),
                 verbose: bool = False, crash_hook=None):
        super().__init__(provider, workspace, tools, budget, verbose)
        self.journal = Journal(journal_path)
        self.crash_hook = crash_hook or (lambda point, detail=None: None)

    def _commit(self, st: RunState, record: dict) -> None:
        """Write-ahead: durable first, then applied to the in-memory state."""
        self.journal.append(record)
        apply(st, record)

    def _finish(self, st: RunState, status: str, answer: str | None, events) -> RunResult:
        self._commit(st, {"type": "run_finished", "status": status, "answer": answer})
        return RunResult(status, answer, st.steps, st.spent, events)

    def run(self, objective: str) -> RunResult:
        events: list[Event] = []

        def log(kind, detail):
            events.append(Event(kind, detail))
            if self.verbose:
                print(f"  [{kind}] {detail}")

        st = RunState()
        records = self.journal.load()
        for r in records:
            apply(st, r)

        if not records:
            self._commit(st, {"type": "run_started", "run_id": uuid.uuid4().hex[:8], "objective": objective})
        else:
            if st.objective != objective:
                raise ValueError("journal belongs to a different objective")
            if st.finished:
                return st.finished
            log("resumed", f"run {st.run_id}: {len(records)} records replayed, step {st.steps}, "
                           f"${st.spent:.4f} spent, {len(st.pending)} call(s) unanswered")

        while True:
            # Finish the last turn's calls (all of them live; the remainder after a crash).
            for call in list(st.pending):
                self._settle(st, call, log)

            # A recorded final answer is final, even if the crash hit before run_finished.
            last = st.history[-1]
            if isinstance(last, AssistantTurn) and not last.tool_calls:
                return self._finish(st, "answered", last.text, events)

            if st.steps >= self.budget.max_steps:
                return self._finish(st, "step_budget", None, events)
            if st.spent >= self.budget.max_cost_usd:
                return self._finish(st, "cost_budget", None, events)

            try:
                c = self.provider.step(st.history, system=self.system, tools=self.specs)
            except OutputError as e:
                self._commit(st, {"type": "model_failed", "cost": e.completion.cost_usd})
                log("model", f"unusable output ({e})")
                return self._finish(st, "aborted", None, events)
            except RequestError as e:
                log("model", f"request rejected ({e})")
                return self._finish(st, "aborted", None, events)
            except LLMError as e:
                # Transient: stop without a run_finished record, so the run stays resumable.
                log("model", f"provider unavailable ({e}); run paused")
                return RunResult("paused", None, st.steps, st.spent, events)

            self.crash_hook("after_model_call")
            self._commit(st, {"type": "model_turn", "step": st.steps + 1, "text": c.text or "",
                              "cost": c.cost_usd,
                              "calls": [{"id": t.id, "name": t.name, "arguments": t.arguments}
                                        for t in c.tool_calls]})
            log("model", f"step {st.steps}: ${c.cost_usd:.4f} "
                         + (f"requests {', '.join(t.name for t in c.tool_calls)}" if c.tool_calls else "answers"))
            self.crash_hook("after_model_turn")

            if not c.tool_calls:
                return self._finish(st, "answered", c.text, events)

    def _settle(self, st: RunState, call: ToolCall, log) -> None:
        """Produce and record exactly one result for one call."""
        if call.id in st.intents:
            result = self._recover(st, call, log)
        else:
            decision = self.authorize(call, st.seen, log)
            if isinstance(decision, ToolResult):
                result = decision
            else:
                tool, args, key = decision
                self._commit(st, {"type": "tool_intent", "call_id": call.id, "repeat_key": key})
                self.crash_hook("after_intent", call)
                result = self.perform(call, tool, args, log, idempotency_key=f"{st.run_id}:{call.id}")
                self.crash_hook("after_execute", call)
        self._commit(st, {"type": "tool_result", "call_id": call.id,
                          "content": result.content, "is_error": result.is_error})

    def _recover(self, st: RunState, call: ToolCall, log) -> ToolResult:
        """An intent without a result: the crash hit while this call was executing."""
        tool = self.tools[call.name]
        args = tool.args.model_validate(call.arguments)
        if tool.effect in ("read", "idempotent", "keyed"):
            log("recovered", f"{call.name}: re-executing ({tool.effect})")
            return self.perform(call, tool, args, log, idempotency_key=f"{st.run_id}:{call.id}")
        log("uncertain", f"{call.name}: outcome unknown, not re-executed ({tool.effect})")
        return ToolResult(call.id,
                          "outcome unknown: the runtime crashed while this action was executing. "
                          "It was NOT re-run, because repeating it would repeat its effect. "
                          "Check whether it took effect before requesting it again.", True)
