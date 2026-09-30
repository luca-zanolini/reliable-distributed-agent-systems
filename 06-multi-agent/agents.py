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

from coordinator import Coordinator
from software import FIXES, TASKS, run_tests

Agent = Iterator[str]


def planner(coord: Coordinator) -> Agent:
    for task_id, (file, description) in TASKS.items():
        coord.add_task(task_id, file, description)
        yield f"planner      adds {task_id} ({description})"


def implementer(name: str, coord: Coordinator, followup: Callable[[str], str] | None = None,
                retry_write: bool = False) -> Agent:
    """Take open tasks until none is left. Options model two realistic behaviours:
    `retry_write` repeats a write whose acknowledgement it did not see, and
    `followup` makes one more edit after finishing all tasks."""
    repo = coord.repo
    while True:
        open_now = coord.open_tasks()
        yield f"{name:<12} reads board: open {[t.id for t in open_now]}"
        if not open_now:
            break
        task = open_now[0]                              # acts on what it read, however old

        coord.claim(task.id, name)
        yield f"{name:<12} claims {task.id}"

        base = repo.head
        text = repo.read(task.file)
        yield f"{name:<12} reads {task.file} @ {base}"

        def write():                                    # the fix is built from the read above
            return repo.write_files({task.file: FIXES[task.id](text),
                                     "CHANGELOG.md": repo.read("CHANGELOG.md") + f"- {task.id} fixed by {name}\n"},
                                    name, f"fix {task.id}")
        new = write()
        yield f"{name:<12} writes fix for {task.id} -> {new}"
        if retry_write:
            new = write()
            yield f"{name:<12} sees no acknowledgement, writes {task.id} again -> {new}"

        coord.complete(task.id, name, base, new)
        yield f"{name:<12} marks {task.id} done"

    if followup:
        v = repo.write("src/lib.rs", followup(repo.read("src/lib.rs")), name, "follow-up edit")
        yield f"{name:<12} makes a follow-up edit -> {v}"


def tester(coord: Coordinator) -> Agent:
    version = coord.repo.head
    files = coord.repo.files(version)
    yield f"tester       checks out {version}"
    passed, failed = run_tests(files)
    coord.report(version, passed, failed, "tester")
    yield f"tester       reports {version}: " + ("all tests pass" if not failed else f"FAIL {failed}")


def reviewer(coord: Coordinator) -> Agent:
    version = coord.repo.head
    yield f"reviewer     reads {version}"
    coord.review(version, True, "reviewer")
    yield f"reviewer     approves {version}"


def sign_off(coord: Coordinator) -> Agent:
    """The coordinator's turn: decide acceptance."""
    version = coord.accept()
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
