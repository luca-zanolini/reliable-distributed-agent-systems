"""The naive failures, each reproduced on demand by an explicit schedule.

Usage: python scenarios.py            (offline, deterministic, free)

Every scenario starts from the same repository and team; only the order in which
agents take steps differs (plus, for two scenarios, one realistic behaviour of one
agent). `diagnose` then reads the record, using the version tags the naive board
ignores, and reports what went wrong.

Step counts per agent: planner 2; implementer 5 per task (read board, claim, read
file, write, mark done) + 1 final read of the board; tester 2; reviewer 2;
coordinator 1. 'name*' runs an agent to its end.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from agents import coordinator, implementer, planner, reviewer, run, tester
from board import Board
from repo import Repo
from software import INITIAL, REGRESSION, run_tests

TASK_TEST = {"T1": "fencing_rejects_stale", "T2": "grant_increments"}


def team(board: Board, impl1: dict | None = None, impl2: dict | None = None) -> dict:
    return {"planner": planner(board),
            "impl-1": implementer("impl-1", board, **(impl1 or {})),
            "impl-2": implementer("impl-2", board, **(impl2 or {})),
            "tester": tester(board), "reviewer": reviewer(board), "coordinator": coordinator(board)}


# The two implementers work concurrently, each on its own task; the second reads
# the file only after the first has written it.
PREFIX = ["planner*", "impl-1", "impl-1", "impl-2", "impl-2"]            # both read the board and claim
IMPL1_TASK = ["impl-1"] * 3                                               # read file, write, mark done
IMPL2_TASK = ["impl-2"] * 3
TAIL = ["impl-1*", "impl-2*", "tester*", "reviewer*", "coordinator*"]
CORRECT = PREFIX + IMPL1_TASK + IMPL2_TASK + TAIL


@dataclass
class Scenario:
    key: str
    title: str
    diagnosis: str                   # the failure in distributed-systems terms
    remedy: str                      # the mechanism that prevents it, and the stage that adds it
    schedule: list[str]
    impl1: dict = field(default_factory=dict)
    impl2: dict = field(default_factory=dict)
    crash_after: int | None = None   # coordinator crash after this many schedule entries


SCENARIOS = [
    Scenario("a", "Two workers claim the same task",
             "race on a check-then-act: both read 'T1 open' before either claims it",
             "atomic conditional claim (compare-and-set), then leases and fencing tokens (Stage 8)",
             ["planner*", "impl-1", "impl-2", "impl-1", "impl-2"] + IMPL1_TASK + IMPL2_TASK + TAIL),
    Scenario("b", "The reviewer approves a stale version",
             "stale read: the review is about an older version than the one accepted",
             "version-tagged evidence, checked at acceptance (Stage 8)",
             PREFIX + IMPL1_TASK + ["reviewer"] + IMPL2_TASK + ["impl-1*", "impl-2*", "tester*", "reviewer*", "coordinator*"]),
    Scenario("c", "Tested version A, accepted version B",
             "evidence and decision refer to different versions; the accepted one was never tested",
             "accept H only if the passing report and the approval are both for H and H is still the head (Stage 8)",
             PREFIX + IMPL1_TASK + IMPL2_TASK + ["impl-1*", "tester*", "impl-2*", "reviewer*", "coordinator*"],
             impl2={"followup": REGRESSION}),
    Scenario("d", "A finished action is repeated",
             "at-least-once retry without deduplication: a write whose acknowledgement was lost is redone",
             "idempotency key per action, deduplicated by the receiver (Stages 3 and 8)",
             PREFIX + ["impl-1"] * 4 + IMPL2_TASK + TAIL,
             impl1={"retry_write": True}),
    Scenario("e", "Conflicting edits to the same file",
             "lost update: both read version H0, the second write discards the first",
             "optimistic concurrency: a write names its base version and is rejected if the head moved (Stage 8)",
             PREFIX + ["impl-1", "impl-2"] + ["impl-1"] * 2 + ["impl-2"] * 2 + TAIL),
    Scenario("f", "The coordinator crashes",
             "volatile coordinator state: after a restart, completed and in-progress work is forgotten and redone",
             "durable board (journal, Stage 3 applied to the coordinator), then replication by consensus (Stage 9)",
             PREFIX + IMPL1_TASK + ["planner*", "impl-1*", "impl-2*", "tester*", "reviewer*", "coordinator*"],
             crash_after=len(PREFIX) + 3),
]


def execute(s: Scenario | None) -> tuple[list[str], Board, list[Board]]:
    """Run a scenario (None = the correct baseline). Returns timeline, final board, crashed boards."""
    repo = Repo(INITIAL)
    board = Board(repo)
    agents = team(board, s.impl1 if s else None, s.impl2 if s else None)
    schedule = s.schedule if s else CORRECT
    crashed = []
    if s and s.crash_after is not None:
        timeline = run(agents, schedule[:s.crash_after])
        timeline.append("*** coordinator crashes: the board is lost; the repository survives ***")
        crashed.append(board)
        board = Board(repo)                              # restarted coordinator, empty board
        agents = team(board)                             # everyone reconnects to it
        timeline += run(agents, schedule[s.crash_after:])
    else:
        timeline = run(agents, schedule)
    return timeline, board, crashed


def diagnose(board: Board, crashed: list[Board] = ()) -> list[str]:
    """What the version-tagged record shows, whether or not the naive board noticed."""
    repo, found = board.repo, []
    events = [e for b in [*crashed, board] for e in b.events]
    head_failed = run_tests(repo.files())[1]

    for e in events:
        if e["what"] == "claim" and e["status_before"] == "claimed" and e["owner_before"] != e["who"]:
            found.append(f"{e['task']} had two owners at once ({e['owner_before']}, then {e['who']})")
    changelog = Counter(line.split(" fixed")[0] for line in repo.read("CHANGELOG.md").splitlines()[1:])
    for task, n in sorted(changelog.items()):
        if n > 1:
            found.append(f"{task.lstrip('- ')} was carried out {n} times (duplicate changelog entries)")
    for t in board.tasks.values():
        if t.status == "done" and TASK_TEST[t.id] in head_failed:
            found.append(f"{t.id} is marked done, but its fix is not in the head (overwritten or reverted after completion)")
    if crashed:
        before = {tid: t.status for b in crashed for tid, t in b.tasks.items()}
        found.append(f"coordinator restart forgot task states {before}; the new board started from scratch")

    if board.accepted:
        v = board.accepted
        if not any(r["version"] == v and not r["failed"] for r in board.reports):
            tested = sorted({r["version"] for r in board.reports})
            found.append(f"accepted {v} without a passing test report for it (reports are for {tested})")
        if not any(r["version"] == v and r["approved"] for r in board.reviews):
            reviewed = sorted({r["version"] for r in board.reviews})
            found.append(f"accepted {v} without a review of it (reviews are for {reviewed})")
        failing = run_tests(repo.files(v))[1]
        if failing:
            found.append(f"the accepted version fails {failing}")
    else:
        found.append("nothing accepted: the run ended with the work unverified or incomplete")
    return found


if __name__ == "__main__":
    for s in [None, *SCENARIOS]:
        timeline, board, crashed = execute(s)
        title = f"({s.key}) {s.title}" if s else "Baseline: correct concurrent schedule"
        print(f"\n{'=' * 90}\n{title}\n{'=' * 90}")
        print("\n".join("  " + line for line in timeline))
        found = diagnose(board, crashed)
        print("\n  record shows: " + ("no anomaly" if not found else ""))
        for f in found:
            print(f"    - {f}")
        if s:
            print(f"  diagnosis:    {s.diagnosis}\n  remedy:       {s.remedy}")
