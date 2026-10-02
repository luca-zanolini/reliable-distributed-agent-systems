"""A worker or a checker: claims a task, keeps its lease alive, does the job, reports.

Configured entirely by its environment (in the lab it runs in its own container):
  ID, SECRET, ROLE         identity, shared secret with the ledger, "worker" or "checker"
  LEDGER_URL               the only service it can reach
  WORK_S                   how long one job takes (simulated: no model is called)

A worker drafts the email for its task. The first draft of every fourth task is
deliberately poor (no greeting), so checkers reject it and the task is redone. A task
whose address is invalid is given up as a permanent failure.

A checker approves a draft that greets the customer and names the subject, and its
verdict names the exact draft it judged.

While it holds a task, a background thread renews the lease every third of the lease
time. A STALE answer to anything means the task was taken away: the agent stops and
discards its work. A finished result is reported until the ledger answers. It keeps a local journal (agent.jsonl in its working directory)
of what it did and what it was told, for the experiments to read.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from ledger import fingerprint
from wire import VERSION, Claim, Client, GiveUp, Renew, RPCError, Submit, Verdict

ID, ROLE = os.environ["ID"], os.environ["ROLE"]
WORK_S = float(os.environ.get("WORK_S", "0.5"))
ledger = Client(os.environ["LEDGER_URL"], ID, os.environ["SECRET"])


def journal(**entry):
    with open("agent.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")
        f.flush()
        os.fsync(f.fileno())


class Lease:
    """Renews a held task until stopped; remembers if the ledger said STALE."""

    def __init__(self, task: str, token: int, lease_s: float):
        self.task, self.token, self.stale = task, token, False
        self.done = threading.Event()
        threading.Thread(target=self.run, args=(lease_s / 3,), daemon=True).start()

    def run(self, every: float):
        while not self.done.wait(every):
            try:
                ledger.call("/renew", Renew(v=VERSION, who=ID, task=self.task, token=self.token))
            except RPCError as e:
                if e.status == 409:
                    self.stale = True
                    return
            except OSError:
                pass                                 # ledger unreachable: keep trying, keep working

    def stop(self):
        self.done.set()


def report(path: str, message, task: str, token: int) -> None:
    """Report until the ledger answers: finished work is not dropped because the ledger
    was briefly unreachable. The same body is resent, so a repeat is recognised."""
    while True:
        try:
            out = ledger.call(path, message)
            journal(task=task, token=token, state="reported", path=path, duplicate=out.get("duplicate", False))
            return
        except RPCError as e:
            journal(task=task, token=token, state="fenced" if e.status == 409 else "refused",
                    path=path, status=e.status)
            return
        except OSError as e:
            journal(task=task, token=token, state="ledger_unreachable", path=path, error=type(e).__name__)
            time.sleep(0.5)


def draft_for(grant: dict) -> str:
    task, subject, attempt = grant["task"], grant["body"]["subject"], grant["attempt"]
    if int(task[1:]) % 4 == 0 and attempt == 1:
        return f"{subject} attached."                               # poor: rejected by the checker
    return f"Dear customer,\n\nPlease find {subject} attached.\n\nBest regards,\n{ID}"


def acceptable(draft: str, subject: str) -> bool:
    return draft.startswith("Dear") and subject in draft


def work(grant: dict) -> None:
    task, token = grant["task"], grant["token"]
    if "@" not in grant["body"]["to"]:
        report("/give_up", GiveUp(v=VERSION, who=ID, task=task, token=token, permanent=True,
                                  reason="invalid address"), task, token)
        return
    lease = Lease(task, token, grant["lease_s"])
    deadline = time.monotonic() + WORK_S
    while time.monotonic() < deadline and not lease.stale:
        time.sleep(0.02)                                            # the work itself
    lease.stop()
    if lease.stale:
        journal(task=task, token=token, state="fenced", path="/renew")
        return                                                      # taken away: discard the work
    if ROLE == "worker":
        report("/submit", Submit(v=VERSION, who=ID, task=task, token=token, draft=draft_for(grant)),
               task, token)
    else:
        draft = grant["draft"]
        ok = acceptable(draft, grant["body"]["subject"])
        report("/verdict", Verdict(v=VERSION, who=ID, task=task, token=token, draft_hash=fingerprint(draft),
                                   approve=ok, reason="ok" if ok else "no greeting"), task, token)


def main():
    while True:
        try:
            grant = ledger.call("/claim", Claim(v=VERSION, who=ID, request_id=uuid.uuid4().hex))
        except OSError:
            journal(state="ledger_unreachable")
            time.sleep(0.5)
            continue
        if grant.get("task") is None:
            if grant.get("all_final"):
                journal(state="exit")
                return
            time.sleep(0.05)
            continue
        journal(task=grant["task"], token=grant["token"], state="claimed", attempt=grant["attempt"])
        work(grant)


if __name__ == "__main__":
    main()
