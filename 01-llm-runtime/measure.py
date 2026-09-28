"""Free-form vs structured output on one extraction task, N runs each.

Usage: python measure.py [N] [MODEL]      (defaults: 10, claude-opus-4-8)

Reports, per arm: output tokens, latency, cost per call, and the number of
distinct answers across the N runs. Every call goes through the adapter.
"""

import statistics
import sys

from pydantic import BaseModel

from llm import AnthropicProvider, Completion


class TestReport(BaseModel):
    passed: bool
    tests_run: int
    failures: list[str]


FAKE_LOG = """
running 5 tests
test quorum::intersection ... ok
test leases::expiry ... ok
test fencing::stale_writer_rejected ... FAILED
test ledger::no_double_assign ... ok
test retry::idempotent ... ok

failures: fencing::stale_writer_rejected — assertion failed: expected Rejected, got Accepted
test result: FAILED. 4 passed; 1 failed
"""


def run_arm(provider, label: str, prompt: str, n: int, schema=None) -> list[Completion]:
    print(f"== {label} ==")
    runs = []
    for i in range(n):
        c = provider.complete(prompt, schema=schema, max_tokens=1000)
        print(f"run={i} latency={c.latency_s:.2f}s in={c.input_tokens} "
              f"out={c.output_tokens} cost=${c.cost_usd:.6f} stop={c.stop}")
        runs.append(c)
    return runs


def summarize(label: str, runs: list[Completion]) -> None:
    outs = [c.output_tokens for c in runs]
    lats = [c.latency_s for c in runs]
    answers = {c.text if c.parsed is None else c.parsed.model_dump_json() for c in runs}
    print(f"{label:<12} in={runs[0].input_tokens:<4} "
          f"out={min(outs)}–{max(outs)} (mean {statistics.mean(outs):.0f})  "
          f"latency={min(lats):.2f}–{max(lats):.2f}s (mean {statistics.mean(lats):.2f})  "
          f"cost/call=${statistics.mean(c.cost_usd for c in runs):.6f}  "
          f"distinct answers={len(answers)}/{len(runs)}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    provider = AnthropicProvider(model=sys.argv[2]) if len(sys.argv) > 2 else AnthropicProvider()

    free = run_arm(provider, "free-form", f"Give me a summary of the log:\n{FAKE_LOG}", n)
    structured = run_arm(provider, "structured", f"Extract a test report from this log:\n{FAKE_LOG}", n,
                         schema=TestReport)

    print(f"\n== summary (model={provider.model}, N={n}) ==")
    summarize("free-form", free)
    summarize("structured", structured)
    total = sum(c.cost_usd for c in free + structured)
    print(f"total spend: ${total:.4f}")
