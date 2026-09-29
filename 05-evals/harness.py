"""Run the suite under one configuration, N repeats per task, one record per run.

Usage: python harness.py CONFIG [--repeats N] [--out DIR]
       CONFIG is a key of CONFIGS below. Live runs call the model and cost money.

Each run starts from a fresh copy of the task's fixture. Each record carries
enough to judge the run without re-running it and to reproduce its conditions:
the configuration and its hash, the suite's hash, the outcome, the validator's
verdict and reason, a failure category, latency, cost, tokens, tool-call counts,
hashes of every file in the workspace afterwards, and provenance (repository
commit and cleanliness, Python and SDK versions). Traces go to DIR/traces/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from tracing import Trace, TracedAgent, TracedProvider  # first: sets the import path
from agent import SYSTEM, Budget  # noqa: E402
from tasks import SUITE, Task  # noqa: E402
from tools import Workspace  # noqa: E402

HERE = Path(__file__).resolve().parent

TERSE = ("You work in a sandboxed directory through the tools provided. Read before you "
         "answer. Follow the requested answer format exactly.")


@dataclass(frozen=True)
class Config:
    name: str
    model: str
    system: str = SYSTEM
    max_steps: int = 10
    max_cost_usd: float = 0.25

    def id(self) -> str:
        return _sha(json.dumps(asdict(self), sort_keys=True))[:12]


CONFIGS = {
    "opus48-default": Config("opus48-default", "claude-opus-4-8"),
    "sonnet5-default": Config("sonnet5-default", "claude-sonnet-5"),
    "opus48-terse": Config("opus48-terse", "claude-opus-4-8", system=TERSE),
}


def _sha(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def suite_id(suite: list[Task]) -> str:
    return _sha(json.dumps([[t.id, t.objective, sorted(t.files().items())] for t in suite]))[:12]


def provenance() -> dict:
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True).stdout.strip()
        except OSError:
            return ""
    try:
        import anthropic
        sdk = anthropic.__version__
    except ImportError:
        sdk = None
    return {"commit": git("rev-parse", "--short", "HEAD"), "dirty": bool(git("status", "--porcelain")),
            "python": platform.python_version(), "anthropic_sdk": sdk}


def category(status: str, passed: bool, task: Task) -> str:
    if status == "answered":
        if passed:
            return "passed"
        return "claim_without_evidence" if task.validator.__name__ == "fencing_fixed" else "wrong_answer"
    return status                                   # step_budget | cost_budget | aborted


def run_once(task: Task, config: Config, provider, repeat: int, suite: str, prov: dict,
             out: Path) -> dict:
    run_id = uuid.uuid4().hex[:12]
    trace = Trace(run_id)
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        for rel, text in task.files().items():
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text(text)
        agent = TracedAgent(TracedProvider(provider, trace), Workspace(ws), list(task.tools),
                            Budget(max_steps=config.max_steps, max_cost_usd=config.max_cost_usd),
                            system=config.system, trace=trace)
        t0 = time.perf_counter()
        result = agent.run(task.objective)
        latency = time.perf_counter() - t0
        passed, reason = task.validator(result.answer, ws) if result.status == "answered" else (False, result.status)
        artifacts = {p.relative_to(ws).as_posix(): _sha(p.read_bytes())[:16]
                     for p in sorted(ws.rglob("*")) if p.is_file()}
    trace.write(out / "traces" / f"{run_id}.jsonl")
    return {
        "run_id": run_id, "task_id": task.id, "repeat": repeat,
        "config": config.name, "config_id": config.id(), "model": config.model,
        "system_prompt_sha": _sha(config.system)[:12], "suite_id": suite,
        "tools": [t.name for t in task.tools],
        "status": result.status, "answer": result.answer,
        "validator_passed": passed, "validator_reason": reason,
        "category": category(result.status, passed, task),
        "latency_s": round(latency, 3), "cost_usd": round(result.cost_usd, 6), "steps": result.steps,
        **trace.totals(), "artifacts": artifacts, "provenance": prov,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def run_suite(config: Config, provider_for, repeats: int, out: Path, suite: list[Task] = SUITE) -> Path:
    """provider_for(task, repeat) returns the provider for one run."""
    out.mkdir(parents=True, exist_ok=True)
    sid, prov = suite_id(suite), provenance()
    results = out / f"{config.name}.jsonl"
    with open(results, "a") as f:
        for task in suite:
            for k in range(repeats):
                rec = run_once(task, config, provider_for(task, k), k, sid, prov, out)
                f.write(json.dumps(rec) + "\n")
                f.flush()
                print(f"  {config.name:<16} {task.id:<9} #{k}  {rec['category']:<22} "
                      f"${rec['cost_usd']:.4f}  {rec['latency_s']:.1f}s  tools={rec['tool_calls']}")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("config", choices=sorted(CONFIGS))
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default=str(HERE / "results"))
    a = ap.parse_args()
    from llm import AnthropicProvider
    cfg = CONFIGS[a.config]
    provider = AnthropicProvider(cfg.model)
    path = run_suite(cfg, lambda task, k: provider, a.repeats, Path(a.out))
    print(f"results: {path}")
