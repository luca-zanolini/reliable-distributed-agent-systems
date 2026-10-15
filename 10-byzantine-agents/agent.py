"""A worker or a checker.

Configured entirely by its environment:
  ID, KEY, ROLE      identity, its own private key, "worker" or "checker"
  LEDGER_URLS        the ledger front-ends, comma-separated; it starts with the first
  WORK_S             how long one job takes (simulated: no model is called)
  KIND               for a checker: "rule" or "llm" (below)
  BYZANTINE          "inject" for a worker, "approve-all" for a checker: the liars

A worker claims a task, keeps its lease alive, drafts the email, submits it (Stage 9).
The first draft of every fourth task is deliberately poor, so that it is rejected and
redone; a task whose address is invalid is given up. A Byzantine worker ("inject")
appends, to every draft, a note addressed to the reviewers telling them to approve it.

A checker asks for a draft it has not voted on, judges it, and sends a verdict it signs
itself (evidence.judge): this text, for this task, approved or not. Two kinds (drafts.py):
"rule", a deterministic program, and "llm", a stand-in for a language-model checker that
obeys instructions it finds in the text. Three "llm" checkers with the same weakness are
three copies of one failure.

A Byzantine checker ("approve-all") approves everything it is shown.

Every agent keeps a journal (agent.jsonl) of what it did and what it was told, including
the receipts the front-ends signed; the experiments and the audit read it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

import evidence as E
from drafts import INJECTION, llm_check, rule_check, template
from wire import VERSION, Claim, Client, GiveUp, Msg, Renew, RPCError, Submit, Vote

ID, ROLE, KEY = os.environ["ID"], os.environ["ROLE"], os.environ["KEY"]
WORK_S = float(os.environ.get("WORK_S", "0.5"))
KIND = os.environ.get("KIND", "rule")
BYZANTINE = os.environ.get("BYZANTINE", "")
ledger = Client(os.environ["LEDGER_URLS"], ID, KEY)


def journal(**entry):
    with open("agent.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")
        f.flush()
        os.fsync(f.fileno())


# --- drafting and judging (the rules themselves are in drafts.py) ---------------------------
def judge(draft: str, subject: str) -> tuple[bool, str]:
    if BYZANTINE == "approve-all":
        return True, "ok"
    ok = rule_check(draft, subject) if KIND == "rule" else llm_check(draft, subject)
    return ok, "ok" if ok else "not acceptable"


def draft_for(grant: dict) -> str:
    task, subject, attempt = grant["task"], grant["signed_task"]["body"]["subject"], grant["attempt"]
    if BYZANTINE == "inject":
        return template(subject, ID) + INJECTION
    if task[1:].isdigit() and int(task[1:]) % 4 == 0 and attempt == 1:
        return f"{subject} attached."                     # poor: honest checkers reject it
    return template(subject, ID)


# --- the worker ----------------------------------------------------------------------------
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
                pass

    def stop(self):
        self.done.set()


def report(path: str, message, **ctx) -> dict | None:
    """Report until the ledger answers. The same body is resent, so a repeat is recognised."""
    while True:
        try:
            out = ledger.call(path, message)
            journal(**ctx, state="reported", path=path, via=out.get("_via"),
                    duplicate=out.get("duplicate", False), receipt=out.get("receipt"),
                    request=out.get("_request"))
            return out
        except RPCError as e:
            journal(**ctx, state="fenced" if e.status == 409 else "refused", path=path, status=e.status,
                    error=e.payload.get("error"))
            return None
        except OSError as e:
            journal(**ctx, state="ledger_unreachable", path=path, error=type(e).__name__)
            time.sleep(0.5)


def work(grant: dict) -> None:
    task, token = grant["task"], grant["token"]
    if "@" not in grant["signed_task"]["body"]["to"]:
        report("/give_up", GiveUp(v=VERSION, who=ID, task=task, token=token, permanent=True,
                                  reason="invalid address"), task=task, token=token)
        return
    lease = Lease(task, token, grant["lease_s"])
    deadline = time.monotonic() + WORK_S
    while time.monotonic() < deadline and not lease.stale:
        time.sleep(0.02)
    lease.stop()
    if lease.stale:
        journal(task=task, token=token, state="fenced", path="/renew")
        return
    report("/submit", Submit(v=VERSION, who=ID, task=task, token=token, draft=draft_for(grant)),
           task=task, token=token)


def run_worker():
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


# --- the checker -----------------------------------------------------------------------------
def run_checker():
    voted = set()                         # (task, draft hash, round): one vote per submission
    while True:
        try:
            order = ledger.call("/check/next", Msg(v=VERSION, who=ID))
        except OSError:
            time.sleep(0.5)
            continue
        if order.get("task") is None:
            if order.get("all_final"):
                journal(state="exit")
                return
            time.sleep(0.05)
            continue
        if (order["task"], order["draft_hash"], order["round"]) in voted:
            time.sleep(0.1)               # already voted: the ledger has not recorded it (yet)
            continue
        time.sleep(WORK_S)
        signed_task, draft = order["signed_task"], order["draft"]
        approve, reason = judge(draft, signed_task["body"]["subject"])
        v = E.judge(ID, KEY, signed_task, draft, approve)
        out = report("/verdict", Vote(v=VERSION, who=ID, task=order["task"], task_hash=v["task_hash"],
                                      draft_hash=v["draft_hash"], approve=approve, reason=reason, sig=v["sig"]),
                     task=order["task"], verdict=v)
        voted.add((order["task"], order["draft_hash"], order["round"]))
        if out is None:
            time.sleep(0.05)


if __name__ == "__main__":
    run_worker() if ROLE == "worker" else run_checker()
