"""Span-style traces: one timed record per model call and per tool call.

Every span carries the run's id, a sequence number, its kind, its start offset and
duration from the run's clock, and kind-specific attributes (tokens and cost for
model calls; tool name and outcome for tool calls). A trace is written as JSON
lines, so a run can be reconstructed and compared after the fact.

Instrumentation wraps the existing components rather than changing them: the
provider is wrapped in a proxy, and the agent's `execute()` is timed in a subclass.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "02-single-agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "01-llm-runtime"))
from agent import Agent  # noqa: E402


class Trace:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.t0 = time.perf_counter()
        self.spans: list[dict] = []

    def span(self, kind: str, start: float, **attrs) -> None:
        self.spans.append({"run_id": self.run_id, "seq": len(self.spans), "kind": kind,
                           "start_s": round(start - self.t0, 4),
                           "duration_s": round(time.perf_counter() - start, 4), **attrs})

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(s) + "\n" for s in self.spans))

    def totals(self) -> dict:
        model = [s for s in self.spans if s["kind"] == "model_call"]
        tools = [s for s in self.spans if s["kind"] == "tool_call"]
        return {
            "model_calls": len(model),
            "tool_calls": len(tools),
            "tool_calls_refused": sum(1 for s in tools if s["outcome"] in ("rejected", "denied")),
            "tool_calls_failed": sum(1 for s in tools if s["outcome"] == "failed"),
            "input_tokens": sum(s.get("input_tokens", 0) for s in model),
            "output_tokens": sum(s.get("output_tokens", 0) for s in model),
        }


class TracedProvider:
    """Transparent proxy: same interface as the provider it wraps."""

    def __init__(self, inner, trace: Trace):
        self.inner, self.trace = inner, trace

    def step(self, history, **kw):
        start = time.perf_counter()
        try:
            c = self.inner.step(history, **kw)
        except Exception as e:
            self.trace.span("model_call", start, error=type(e).__name__)
            raise
        self.trace.span("model_call", start, input_tokens=c.input_tokens, output_tokens=c.output_tokens,
                        cost_usd=c.cost_usd, stop=c.stop, tool_requests=len(c.tool_calls))
        return c


class TracedAgent(Agent):
    """The Stage 2 agent, with each tool request timed and its outcome recorded."""

    def __init__(self, *args, trace: Trace, **kw):
        super().__init__(*args, **kw)
        self.trace = trace

    def execute(self, call, seen, log):
        start = time.perf_counter()
        outcome = {}

        def spy(kind, detail):                      # capture the gate's verdict for this call
            outcome["kind"] = kind
            log(kind, detail)

        result = super().execute(call, seen, spy)
        self.trace.span("tool_call", start, tool=call.name, outcome=outcome.get("kind", "unknown"),
                        is_error=result.is_error)
        return result
