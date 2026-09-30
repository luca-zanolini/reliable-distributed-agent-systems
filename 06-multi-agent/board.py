"""The coordinator's board: the shared task list, test reports, reviews, acceptance.

Deliberately naive, as the stage requires:
  - claim() assigns an owner without checking the task is still open;
  - complete() marks a task done without checking who owns it or whether it is done;
  - accept() takes the current head once every task is done, some test report
    passed and some review approved, without checking that the report and the
    review are about the version being accepted;
  - the board lives in the coordinator's memory: a coordinator crash loses it,
    while the repository (stored elsewhere) survives.

Every action is recorded with the version it refers to. The naive board ignores
those tags; the scenarios use them to show what went wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from repo import Repo


@dataclass
class Task:
    id: str
    file: str
    description: str
    status: str = "open"                 # open | claimed | done
    owner: str | None = None


class Board:
    def __init__(self, repo: Repo):
        self.repo = repo
        self.tasks: dict[str, Task] = {}
        self.events: list[dict] = []
        self.reports: list[dict] = []
        self.reviews: list[dict] = []
        self.accepted: str | None = None

    def _event(self, **e):
        self.events.append(e)

    # --- planning ------------------------------------------------------------
    def add_task(self, task_id: str, file: str, description: str) -> None:
        self.tasks[task_id] = Task(task_id, file, description)
        self._event(who="planner", what="add", task=task_id)

    # --- ownership (naive) ---------------------------------------------------
    def open_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == "open"]

    def claim(self, task_id: str, worker: str) -> None:
        t = self.tasks[task_id]
        self._event(who=worker, what="claim", task=task_id, status_before=t.status, owner_before=t.owner)
        t.status, t.owner = "claimed", worker

    def complete(self, task_id: str, worker: str, base: str, result: str) -> None:
        t = self.tasks[task_id]
        self._event(who=worker, what="complete", task=task_id, base=base, result=result,
                    status_before=t.status, owner_before=t.owner)
        t.status, t.owner = "done", worker

    # --- verification --------------------------------------------------------
    def report(self, version: str, passed: list[str], failed: list[str], by: str) -> None:
        self.reports.append({"version": version, "passed": passed, "failed": failed, "by": by})
        self._event(who=by, what="report", version=version, ok=not failed)

    def review(self, version: str, approved: bool, by: str) -> None:
        self.reviews.append({"version": version, "approved": approved, "by": by})
        self._event(who=by, what="review", version=version, approved=approved)

    # --- acceptance (naive) --------------------------------------------------
    def accept(self) -> str | None:
        ready = (self.tasks and all(t.status == "done" for t in self.tasks.values())
                 and any(not r["failed"] for r in self.reports)
                 and any(r["approved"] for r in self.reviews))
        self.accepted = self.repo.head if ready else None
        self._event(who="coordinator", what="accept", version=self.accepted)
        return self.accepted
