"""Scripted agents and a deterministic scheduler.

Each agent is a generator: it performs one action on shared state (the board or
the repository), then pauses at `yield`, returning a line describing what it did.
The scheduler resumes agents in an explicit order, so any interleaving of their
actions can be reproduced exactly: the same technique as a controlled schedule in
a distributed-systems simulation.

The agents are deliberately naive: they act on what they read, without re-checking
it before they write.
"""

from __future__ import annotations

from typing import Callable, Iterator

from board import Board
from software import FIXES, TASKS, run_tests

Agent = Iterator[str]


def planner(board: Board) -> Agent:
    for task_id, (file, description) in TASKS.items():
        board.add_task(task_id, file, description)
        yield f"planner      adds {task_id} ({description})"


def implementer(name: str, board: Board, followup: Callable[[str], str] | None = None) -> Agent:
    repo = board.repo
    open_now = board.open_tasks()
    yield f"{name:<12} reads board: open {[t.id for t in open_now]}"
    if not open_now:
        return
    task = open_now[0]                                  # acts on what it read, however old

    board.claim(task.id, name)
    yield f"{name:<12} claims {task.id}"

    base = repo.head
    text = repo.read(task.file)
    yield f"{name:<12} reads {task.file} @ {base}"

    new = repo.write_files({task.file: FIXES[task.id](text),       # built from the read above
                            "CHANGELOG.md": repo.read("CHANGELOG.md") + f"- {task.id} fixed by {name}\n"},
                           name, f"fix {task.id}")
    yield f"{name:<12} writes fix for {task.id} -> {new}"

    board.complete(task.id, name, base, new)
    yield f"{name:<12} marks {task.id} done"

    if followup:
        v = repo.write(task.file, followup(repo.read(task.file)), name, "follow-up edit")
        yield f"{name:<12} makes a follow-up edit -> {v}"


def tester(board: Board) -> Agent:
    version = board.repo.head
    files = board.repo.files(version)
    yield f"tester       checks out {version}"
    passed, failed = run_tests(files)
    board.report(version, passed, failed, "tester")
    yield f"tester       reports {version}: " + ("all tests pass" if not failed else f"FAIL {failed}")


def reviewer(board: Board) -> Agent:
    version = board.repo.head
    yield f"reviewer     reads {version}"
    board.review(version, True, "reviewer")
    yield f"reviewer     approves {version}"


def coordinator(board: Board) -> Agent:
    version = board.accept()
    yield f"coordinator  " + (f"accepts {version}" if version else "cannot accept: work incomplete or unverified")


def run(agents: dict[str, Agent], schedule: list[str]) -> list[str]:
    """Advance agents in the given order. 'name' = one step; 'name*' = run it to the end."""
    timeline = []
    for entry in schedule:
        name, to_end = entry.rstrip("*"), entry.endswith("*")
        while True:
            step = next(agents[name], None)
            if step is None:
                break
            timeline.append(step)
            if not to_end:
                break
    return timeline
