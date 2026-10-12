"""The ledger's rules: Stage 8's lifecycle, ownership and repeat semantics, unchanged.

    OPEN -claim-> IN_PROGRESS -submit-> UNDER_CHECK -claim-> CHECKING -approve-> APPROVED
      ^                |                     ^                  |                   |
      |  lease expires |                     |   lease expires  |              sending (intent)
      +----------------+                     +------------------+                   v
      +--------------------------- reject ---------------------+             SENDING -sent-> SENT
    FAILED: a permanent error, or the attempt budget used up. SENT and FAILED are final.

What changed in Stage 9 is where the state lives and how it changes. Stage 8 kept the
tasks in one process and changed them by "append to the journal, then apply". Here the
tasks live in a replicated store, and a change is a pure function from the task as read
to the task as it must be written: the front-end reads, applies one of the functions
below, and writes the result back only if nothing changed in between (a conditional
transaction). So these functions hold no state and do no I/O; they are the rules.

A task record carries its epoch (the fencing token of its current or last holder) and the
id of the store lease that keeps its current hold alive.
"""

from __future__ import annotations

import hashlib

OPEN, IN_PROGRESS, UNDER_CHECK, CHECKING = "OPEN", "IN_PROGRESS", "UNDER_CHECK", "CHECKING"
APPROVED, SENDING, SENT, FAILED = "APPROVED", "SENDING", "SENT", "FAILED"
FINAL = {SENT, FAILED}
HELD = {IN_PROGRESS, CHECKING}                    # states in which a claimer holds a lease


class Stale(Exception):
    """The requester no longer holds the task (or never did)."""


class Illegal(Exception):
    """The request is malformed for this task: unknown task, wrong draft, wrong state."""


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def new_task(body: dict) -> dict:
    return {"state": OPEN, "epoch": 0, "owner": None, "lease": 0, "attempts": 0, "body": body,
            "draft": None, "draft_hash": None, "submitted_token": None, "judged": None,
            "approved_text": None, "reason": None}


def wanted(role: str) -> str:
    """The state a claim by this role picks a task from."""
    return OPEN if role == "worker" else UNDER_CHECK


def check_held(t: dict | None, tid: str, worker: str, token: int, states: set[str]) -> dict:
    if t is None:
        raise Illegal(f"unknown task {tid}")
    if t["state"] not in states or t["epoch"] != token or t["owner"] != worker:
        raise Stale(f"{worker} with token {token} does not hold {tid} "
                    f"(state {t['state']}, epoch {t['epoch']})")
    return t


def _reopen(t: dict, reason: str, max_attempts: int) -> dict:
    return {**t, "reason": reason, "state": FAILED if t["attempts"] >= max_attempts else OPEN}


# --- transitions: task as read -> task as it must be written --------------------------
def claimed(t: dict, worker: str, lease: int) -> dict:
    """The epoch only grows: a token is never issued twice for the same task."""
    out = {**t, "epoch": t["epoch"] + 1, "owner": worker, "lease": lease}
    if t["state"] == OPEN:
        return {**out, "state": IN_PROGRESS, "attempts": t["attempts"] + 1}
    if t["state"] == UNDER_CHECK:
        return {**out, "state": CHECKING}
    raise Illegal(f"cannot claim a task in state {t['state']}")


def submitted(t: dict, token: int, draft: str) -> dict:
    return {**t, "state": UNDER_CHECK, "owner": None, "lease": 0, "draft": draft,
            "draft_hash": fingerprint(draft), "submitted_token": token}


def gave_up(t: dict, permanent: bool, reason: str, max_attempts: int) -> dict:
    t = {**t, "owner": None, "lease": 0, "reason": reason}
    return {**t, "state": FAILED} if permanent else _reopen(t, reason, max_attempts)


def judged(t: dict, token: int, approve: bool, reason: str, max_attempts: int) -> dict:
    t = {**t, "owner": None, "lease": 0, "judged": [token, approve]}
    if approve:
        return {**t, "state": APPROVED, "approved_text": t["draft"]}
    return _reopen(t, reason, max_attempts)


def expired(t: dict, max_attempts: int) -> dict:
    """The holder's lease ran out: a check goes back to be checked, work back to be done."""
    t = {**t, "owner": None, "lease": 0}
    if t["state"] == CHECKING:
        return {**t, "state": UNDER_CHECK}
    return _reopen(t, "lease expired", max_attempts)


def sending(t: dict) -> dict:
    if t["state"] != APPROVED:
        raise Illegal(f"cannot send a task in state {t['state']}")
    return {**t, "state": SENDING}


def sent(t: dict) -> dict:
    if t["state"] != SENDING:
        raise Illegal(f"task is {t['state']}, not SENDING")
    return {**t, "state": SENT}


# --- recognising a repeat ---------------------------------------------------------------
def is_repeated_submit(t: dict | None, token: int, draft: str) -> bool:
    return bool(t) and t["submitted_token"] == token and t["draft_hash"] == fingerprint(draft)


def is_repeated_verdict(t: dict | None, token: int, approve: bool) -> bool:
    return bool(t) and t["judged"] == [token, approve]


# --- what the requester gets back -------------------------------------------------------
def grant(tid: str, t: dict, lease_s: float) -> dict:
    return {"task": tid, "token": t["epoch"], "body": t["body"], "attempt": t["attempts"],
            "draft": t["draft"] if t["state"] == CHECKING else None, "lease_s": lease_s}


def send_order(tid: str, t: dict, resend: bool) -> dict:
    return {"task": tid, "key": tid, "to": t["body"]["to"], "subject": t["body"]["subject"],
            "text": t["approved_text"], "resend": resend}
