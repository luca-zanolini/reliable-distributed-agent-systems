"""Summarize result files and compare configurations.

Usage: python report.py RESULTS.jsonl [BASELINE.jsonl]

Per task: pass rate over the repeats, whether all repeats passed (a consistency
measure: pass rate alone hides a task that passes only sometimes), mean cost,
median latency, mean tool calls, and the failure categories seen. Given a
baseline, every task whose pass rate dropped is reported as a regression.
Records from a different suite are refused: comparing different tests is not a
comparison.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path: str | Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def summarize(records: list[dict]) -> dict[str, dict]:
    by_task = defaultdict(list)
    for r in records:
        by_task[r["task_id"]].append(r)
    out = {}
    for task, rs in by_task.items():
        passes = [r["category"] == "passed" for r in rs]
        out[task] = {
            "runs": len(rs),
            "pass_rate": sum(passes) / len(rs),
            "all_passed": all(passes),
            "mean_cost_usd": statistics.mean(r["cost_usd"] for r in rs),
            "median_latency_s": statistics.median(r["latency_s"] for r in rs),
            "mean_tool_calls": statistics.mean(r["tool_calls"] for r in rs),
            "failures": dict(Counter(r["category"] for r in rs if r["category"] != "passed")),
        }
    return out


def overall(records: list[dict]) -> dict:
    return {"runs": len(records),
            "pass_rate": sum(r["category"] == "passed" for r in records) / len(records),
            "total_cost_usd": sum(r["cost_usd"] for r in records)}


def regressions(candidate: list[dict], baseline: list[dict]) -> list[str]:
    if {r["suite_id"] for r in candidate} != {r["suite_id"] for r in baseline}:
        raise ValueError("results come from different suites; not comparable")
    a, b = summarize(baseline), summarize(candidate)
    return [f"{t}: pass rate {a[t]['pass_rate']:.0%} -> {b[t]['pass_rate']:.0%}"
            for t in a if t in b and b[t]["pass_rate"] < a[t]["pass_rate"]]


def table(records: list[dict]) -> str:
    rows = ["| task | pass rate | all repeats | mean cost | median latency | mean tool calls | failures |",
            "|---|---|---|---|---|---|---|"]
    for t, s in summarize(records).items():
        fails = ", ".join(f"{k}×{v}" for k, v in s["failures"].items()) or "—"
        rows.append(f"| {t} | {s['pass_rate']:.0%} | {'yes' if s['all_passed'] else 'no'} | "
                    f"${s['mean_cost_usd']:.4f} | {s['median_latency_s']:.1f} s | "
                    f"{s['mean_tool_calls']:.1f} | {fails} |")
    o = overall(records)
    rows.append(f"\n{records[0]['config']} ({records[0]['model']}): {o['runs']} runs, "
                f"pass rate {o['pass_rate']:.0%}, total ${o['total_cost_usd']:.3f}")
    return "\n".join(rows)


if __name__ == "__main__":
    cand = load(sys.argv[1])
    print(table(cand))
    if len(sys.argv) > 2:
        base = load(sys.argv[2])
        print(f"\nagainst {base[0]['config']}:")
        found = regressions(cand, base)
        print("\n".join(f"  REGRESSION {x}" for x in found) or "  no regressions")
