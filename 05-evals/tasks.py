"""The evaluation suite: tasks with ground truth and deterministic validators.

A task is a fixture (the files the workspace starts with), an objective, the tools
it may use, and a validator. A validator sees the agent's final answer AND the
workspace after the run, and returns (passed, reason). Where the task asks for an
effect, the validator checks the effect (the file on disk), never the agent's
claim about it.

Validators here are keyword and pattern checks. They are deterministic and cheap,
but can err both ways: a false positive when an answer contains the right words in
a wrong statement, a false negative when a correct answer is phrased differently.
Objectives that fix an answer format ("answer as ran=N failed=M") reduce the second.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "02-single-agent"))
from tools import READ_ONLY, WRITE_FILE, Tool  # noqa: E402

Validator = Callable[[str | None, Path], tuple[bool, str]]

REPO = {
    "test-output.log": """\
running 5 tests
test quorum::intersection ... ok
test leases::expiry ... ok
test fencing::stale_writer_rejected ... FAILED
test ledger::no_double_assign ... ok
test retry::idempotent ... ok

failures: fencing::stale_writer_rejected — assertion failed: expected Rejected, got Accepted
test result: FAILED. 4 passed; 1 failed
""",
    "src/fencing.rs": """\
/// Storage-side fencing: accept a write only if its token is at least as new
/// as the newest token seen so far; reject writes from superseded holders.
pub struct Fence { highest_seen: u64 }

pub enum Decision { Accepted, Rejected }

impl Fence {
    pub fn check(&mut self, token: u64) -> Decision {
        if token >= self.highest_seen {
            self.highest_seen = token;
            Decision::Accepted
        } else {
            Decision::Accepted
        }
    }
}
""",
    "src/leases.rs": """\
/// Leases grant exclusive ownership for a bounded time; each grant carries a
/// strictly increasing fencing token.
pub fn grant(next_token: &mut u64) -> u64 { *next_token += 1; *next_token }
""",
    "metrics/lease-durations-ms.txt": "1500\n2250\n4000\n5000\n",
}


@dataclass(frozen=True)
class Task:
    id: str
    objective: str
    validator: Validator
    tools: tuple[Tool, ...] = tuple(READ_ONLY)
    fixture: dict = None

    def files(self) -> dict:
        return self.fixture or REPO


def mentions(*needles: str) -> Validator:
    def check(answer, ws):
        missing = [n for n in needles if n.lower() not in (answer or "").lower()]
        return (not missing, f"answer missing {missing}" if missing else "all expected facts present")
    return check


def pattern(regex: str) -> Validator:
    def check(answer, ws):
        ok = re.search(regex, answer or "") is not None
        return ok, "format and values match" if ok else f"answer does not match /{regex}/"
    return check


def fencing_fixed(answer, ws: Path):
    """Evidence, not claim: the stale branch of Fence::check must now reject."""
    src = (ws / "src" / "fencing.rs").read_text()
    m = re.search(r"\}\s*else\s*\{(.*?)\}", src, re.S)
    if not m:
        return False, "else branch not found in src/fencing.rs"
    if "Decision::Rejected" not in m.group(1):
        return False, "src/fencing.rs: stale branch still does not return Rejected"
    if "Decision::Accepted" not in src.split("else")[0]:
        return False, "src/fencing.rs: current-token branch no longer accepts"
    return True, "stale branch returns Rejected; current branch still accepts"


SUITE = [
    Task("diagnose", "The test suite in this repository is failing. Which test failed, and which "
         "source file is responsible? Answer in one or two sentences.",
         mentions("stale_writer_rejected", "src/fencing.rs")),
    Task("count", "How many tests ran and how many failed? Answer exactly in the form "
         "ran=N failed=M and nothing else.",
         pattern(r"^\s*ran=5 failed=1\s*$")),
    Task("locate", "In which file and on which line is the function `grant` defined? "
         "Answer exactly as path:line and nothing else.",
         pattern(r"^\s*src/leases\.rs:3\s*$")),
    Task("compute", "metrics/lease-durations-ms.txt lists lease durations in milliseconds. "
         "What is their total in seconds? Answer with the number only.",
         pattern(r"^\s*12\.75\s*$")),
    Task("fix", "The fencing test fails. Fix the bug in src/fencing.rs by rewriting the file "
         "with the smallest correct change, then reply with one sentence describing the fix.",
         fencing_fixed, tools=tuple(READ_ONLY) + (WRITE_FILE,)),
]
