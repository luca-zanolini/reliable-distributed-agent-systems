"""Re-apply the current validators to stored answers, without re-running the agent.

Usage: python regrade.py RESULTS.jsonl      -> writes RESULTS.regraded.jsonl

When a validator or the failure taxonomy changes, the recorded outputs can be
graded again for free. The original file is never modified: it is the record of
what was observed. Tasks whose validator inspects the workspace (evidence
validators) cannot be regraded from the answer alone; their verdicts are kept.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from harness import category, provenance  # first: sets the import path
from tasks import SUITE

WORKSPACE_VALIDATORS = {"fencing_fixed"}


def regrade(records: list[dict]) -> list[dict]:
    tasks = {t.id: t for t in SUITE}
    grader = provenance()["commit"]
    out = []
    for r in records:
        task = tasks[r["task_id"]]
        if r["status"] == "answered" and task.validator.__name__ not in WORKSPACE_VALIDATORS:
            with tempfile.TemporaryDirectory() as tmp:
                passed, reason = task.validator(r["answer"], Path(tmp))
            r = {**r, "validator_passed": passed, "validator_reason": reason,
                 "category": category(r["status"], passed, reason, task),
                 "regraded_by_commit": grader, "original_category": r["category"]}
        out.append(r)
    return out


if __name__ == "__main__":
    src = Path(sys.argv[1])
    dst = src.with_suffix(".regraded.jsonl")
    records = [json.loads(l) for l in src.read_text().splitlines() if l.strip()]
    new = regrade(records)
    dst.write_text("".join(json.dumps(r) + "\n" for r in new))
    changed = [(r["task_id"], r["original_category"], r["category"]) for r in new
               if r.get("original_category") not in (None, r["category"])]
    print(f"{dst}: {len(new)} records, {len(changed)} recategorized")
    for c in changed:
        print(f"  {c[0]}: {c[1]} -> {c[2]}")
