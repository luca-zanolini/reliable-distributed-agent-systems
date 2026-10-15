"""The ledger's rules, Stage 10: checking becomes voting, and approval needs a certificate.

    OPEN -claim-> IN_PROGRESS -submit-> UNDER_CHECK --certified--> APPROVED
      ^                |                     |                          |
      |  lease expires |                     | cannot be certified any more  sending (intent)
      +----------------+---------------------+                          v
                                                         SENDING -sent-> SENT
                                                            |
                                                            +-refused-> REFUSED
    FAILED: a permanent error, or the attempt budget used up.
    SENT, FAILED and REFUSED are final.

What changed from Stage 9:

- Tasks are created from documents signed by the issuer; the ledger stores the signed
  document as it came, so the evidence travels with the task.
- A check is no longer a job that one checker claims. Every checker may vote once on the
  current draft, with a verdict it signs (evidence.judge). Votes name the draft's hash,
  so a vote about an earlier draft counts for nothing: no lease or fencing token is
  needed for checkers any more.
- The task is approved as soon as the approving votes satisfy the policy (they form a
  certificate, stored with the task), and sent back for rework as soon as no outcome of
  the votes still missing could satisfy it.
- The mail service may refuse what the ledger approved (the evidence does not hold up):
  REFUSED, final.

As in Stage 9 these are pure functions from the task as read to the task as it must be
written; the front-end applies them with conditional transactions.
"""

from __future__ import annotations

import evidence as E
import sig

OPEN, IN_PROGRESS, UNDER_CHECK = "OPEN", "IN_PROGRESS", "UNDER_CHECK"
APPROVED, SENDING, SENT, FAILED, REFUSED = "APPROVED", "SENDING", "SENT", "FAILED", "REFUSED"
FINAL = {SENT, FAILED, REFUSED}
HELD = {IN_PROGRESS}                            # the only state in which someone holds a lease


class Stale(Exception):
    """The requester no longer holds the task (or never did), or votes on an old draft."""


class Illegal(Exception):
    """The request is malformed for this task."""


def new_task(signed_task: dict) -> dict:
    return {"state": OPEN, "epoch": 0, "owner": None, "lease": 0, "attempts": 0, "task": signed_task,
            "draft": None, "draft_hash": None, "submitted_token": None, "votes": {},
            "certificate": None, "approved_text": None, "reason": None}


def check_held(t: dict | None, tid: str, worker: str, token: int) -> dict:
    if t is None:
        raise Illegal(f"unknown task {tid}")
    if t["state"] != IN_PROGRESS or t["epoch"] != token or t["owner"] != worker:
        raise Stale(f"{worker} with token {token} does not hold {tid} "
                    f"(state {t['state']}, epoch {t['epoch']})")
    return t


def _reopen(t: dict, reason: str, max_attempts: int) -> dict:
    return {**t, "reason": reason, "state": FAILED if t["attempts"] >= max_attempts else OPEN}


# --- workers: Stage 9's rules ----------------------------------------------------------
def claimed(t: dict, worker: str, lease: int) -> dict:
    if t["state"] != OPEN:
        raise Illegal(f"cannot claim a task in state {t['state']}")
    return {**t, "epoch": t["epoch"] + 1, "owner": worker, "lease": lease, "state": IN_PROGRESS,
            "attempts": t["attempts"] + 1}


def submitted(t: dict, token: int, draft: str) -> dict:
    return {**t, "state": UNDER_CHECK, "owner": None, "lease": 0, "draft": draft,
            "draft_hash": sig.digest(draft), "submitted_token": token, "votes": {}}


def gave_up(t: dict, permanent: bool, reason: str, max_attempts: int) -> dict:
    t = {**t, "owner": None, "lease": 0, "reason": reason}
    return {**t, "state": FAILED} if permanent else _reopen(t, reason, max_attempts)


def expired(t: dict, max_attempts: int) -> dict:
    return _reopen({**t, "owner": None, "lease": 0}, "lease expired", max_attempts)


# --- checkers: votes ---------------------------------------------------------------------
def voted(t: dict | None, tid: str, verdict: dict, checkers: list[str], policy: dict,
          max_attempts: int) -> dict | None:
    """Record one checker's signed verdict on the current draft. Returns the task as it
    must be written, or None if this exact vote is already recorded (a repeat).
    The caller has checked the verdict's signature."""
    if t is None:
        raise Illegal(f"unknown task {tid}")
    who = verdict["checker"]
    if who not in checkers:
        raise Illegal(f"{who} is not a checker")
    if t["state"] != UNDER_CHECK or verdict["draft_hash"] != t["draft_hash"]:
        raise Stale(f"{tid} is not checking draft {verdict['draft_hash'][:12]} (state {t['state']})")
    if verdict["task_hash"] != E.task_hash(t["task"]):
        raise Illegal(f"verdict names another task")
    earlier = t["votes"].get(who)
    if earlier:
        if earlier["approve"] == verdict["approve"]:
            return None
        raise Illegal(f"{who} already voted {'approve' if earlier['approve'] else 'reject'} on this draft")
    votes = {**t["votes"], who: verdict}
    t = {**t, "votes": votes}
    approvers = {c for c, v in votes.items() if v["approve"]}
    if E.certifies(approvers, policy):
        return {**t, "state": APPROVED, "approved_text": t["draft"],
                "certificate": [votes[c] for c in sorted(approvers)]}
    undecided = set(checkers) - set(votes)
    if not E.certifies(approvers | undecided, policy):
        rejecters = sorted(c for c, v in votes.items() if not v["approve"])
        return _reopen(t, f"rejected by {', '.join(rejecters)}", max_attempts)
    return t


def needs_vote(t: dict, checker: str) -> bool:
    return t["state"] == UNDER_CHECK and checker not in t["votes"]


# --- the sender --------------------------------------------------------------------------
def sending(t: dict) -> dict:
    if t["state"] != APPROVED:
        raise Illegal(f"cannot send a task in state {t['state']}")
    return {**t, "state": SENDING}


def sent(t: dict) -> dict:
    if t["state"] != SENDING:
        raise Illegal(f"task is {t['state']}, not SENDING")
    return {**t, "state": SENT}


def refused(t: dict, reason: str) -> dict:
    if t["state"] != SENDING:
        raise Illegal(f"task is {t['state']}, not SENDING")
    return {**t, "state": REFUSED, "reason": reason}


# --- recognising a repeat ------------------------------------------------------------------
def is_repeated_submit(t: dict | None, token: int, draft: str) -> bool:
    return bool(t) and t["submitted_token"] == token and t["draft_hash"] == sig.digest(draft)


# --- what the requester gets back ------------------------------------------------------------
def grant(tid: str, t: dict, lease_s: float) -> dict:
    return {"task": tid, "token": t["epoch"], "signed_task": t["task"], "attempt": t["attempts"],
            "lease_s": lease_s}


def check_order(tid: str, t: dict) -> dict:
    """round: the submission this draft came from. The same text can be submitted again
    (a retry by a worker that writes identically); each submission is checked afresh."""
    return {"task": tid, "signed_task": t["task"], "draft": t["draft"], "draft_hash": t["draft_hash"],
            "round": t["submitted_token"]}


def send_order(tid: str, t: dict, resend: bool) -> dict:
    return {"task": tid, "key": tid, "signed_task": t["task"], "text": t["approved_text"],
            "certificate": t["certificate"], "resend": resend}
