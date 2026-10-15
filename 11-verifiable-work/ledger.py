"""The ledger's rules, Stage 11: work is a version, and approval needs evidence of it.

    OPEN -claim-> IN_PROGRESS -submit-> UNDER_CHECK --certified--> APPROVED -> COMMITTING
      ^                |                     |                                    |
      |  lease expires |   tests fail /      |                         committed  |  stale base
      +----------------+---- review rejects -+                         COMMITTED   +--> OPEN (redo)
                                             |                                    |
                                             +--> NEEDS_HUMAN (tests contradict)   +--> REFUSED
    FAILED: a permanent error, or the attempt budget used up.
    COMMITTED, FAILED, REFUSED and NEEDS_HUMAN are final for the ledger.

What changed from Stage 10:

- A worker submits a version (a hash in the repository) and the base it started from;
  never a claim that it works.
- Verifiers send signed attestations: they ran the pinned tests on that exact version.
  Approval needs the passes to satisfy the policy (f+1, or a fail-prone system), as votes
  did in Stage 10.
- Honest verifiers can disagree only if the tests are nondeterministic. A certified pass
  together with a certified failure, or a certified "flaky" (a verifier's own repeated
  runs disagreed), proves exactly that: the task stops at NEEDS_HUMAN. Nothing commits on
  evidence that is not a function of the code.
- Where the issuer's rules require it (the change touches such a path), approval also needs
  an approving review from a reviewer of the right kind; a rejection by such a reviewer sends
  the work back.
- The repository may refuse: a stale base (the code moved on: redo the work on the new
  code) reopens the task; any other refusal is final.

The ledger routes work and collects evidence; it decides nothing about the code. The
repository applies the same rules again, by itself, before anything becomes current.
"""

from __future__ import annotations

import evidence as E

OPEN, IN_PROGRESS, UNDER_CHECK, APPROVED, COMMITTING = "OPEN", "IN_PROGRESS", "UNDER_CHECK", "APPROVED", "COMMITTING"
COMMITTED, FAILED, REFUSED, NEEDS_HUMAN = "COMMITTED", "FAILED", "REFUSED", "NEEDS_HUMAN"
FINAL = {COMMITTED, FAILED, REFUSED, NEEDS_HUMAN}
HELD = {IN_PROGRESS}


class Stale(Exception):
    """The requester no longer holds the task, or speaks about an earlier submission."""


class Illegal(Exception):
    """The request is malformed for this task."""


def new_task(signed_task: dict) -> dict:
    return {"state": OPEN, "epoch": 0, "owner": None, "lease": 0, "attempts": 0, "task": signed_task,
            "base": None, "result": None, "submitted_token": None, "review": "none", "changed": [],
            "attestations": {}, "reviews": {}, "certificate": None, "reason": None}


def check_held(t: dict | None, tid: str, worker: str, token: int) -> dict:
    if t is None:
        raise Illegal(f"unknown task {tid}")
    if t["state"] != IN_PROGRESS or t["epoch"] != token or t["owner"] != worker:
        raise Stale(f"{worker} with token {token} does not hold {tid} (state {t['state']}, epoch {t['epoch']})")
    return t


def _reopen(t: dict, reason: str, max_attempts: int) -> dict:
    return {**t, "reason": reason, "owner": None, "lease": 0,
            "state": FAILED if t["attempts"] >= max_attempts else OPEN}


# --- workers --------------------------------------------------------------------------------
def claimed(t: dict, worker: str, lease: int) -> dict:
    if t["state"] != OPEN:
        raise Illegal(f"cannot claim a task in state {t['state']}")
    return {**t, "epoch": t["epoch"] + 1, "owner": worker, "lease": lease, "state": IN_PROGRESS,
            "attempts": t["attempts"] + 1}


def submitted(t: dict, token: int, base: str, result: str, changed: list[str]) -> dict:
    """The routing information (which review the change needs) comes from the changed paths,
    which the front-end asks the repository for; the repository will check it again anyway."""
    level = E.required_review(changed, t["task"]["body"]["rules"])
    return {**t, "state": UNDER_CHECK, "owner": None, "lease": 0, "base": base, "result": result,
            "submitted_token": token, "changed": changed, "review": "none" if level == "forbidden" else level,
            "attestations": {}, "reviews": {}}


def expired(t: dict, max_attempts: int) -> dict:
    return _reopen(t, "lease expired", max_attempts)


# --- evidence ---------------------------------------------------------------------------------
def _current(t: dict | None, tid: str, doc: dict) -> dict:
    if t is None:
        raise Illegal(f"unknown task {tid}")
    if t["state"] != UNDER_CHECK or (doc.get("base"), doc.get("result")) != (t["base"], t["result"]):
        raise Stale(f"{tid} is not checking that version (state {t['state']})")
    if doc.get("task_hash") != E.task_hash(t["task"]):
        raise Illegal("evidence about another task")
    return t


def attested(t: dict | None, tid: str, a: dict, verifiers: list[str], reviewer_kinds: dict, policy: dict,
             max_attempts: int) -> dict | None:
    t = _current(t, tid, a)
    who = a["verifier"]
    if who not in verifiers:
        raise Illegal(f"{who} is not a verifier")
    if a.get("tests") != t["task"]["body"]["tests"]:
        raise Illegal("the attestation names other tests than the pinned ones")
    earlier = t["attestations"].get(who)
    if earlier:
        if earlier["outcome"] == a["outcome"]:
            return None
        raise Illegal(f"{who} already attested {earlier['outcome']} for this version")
    return decide({**t, "attestations": {**t["attestations"], who: a}}, verifiers, reviewer_kinds, policy, max_attempts)


def reviewed(t: dict | None, tid: str, r: dict, verifiers: list[str], reviewer_kinds: dict, policy: dict,
             max_attempts: int) -> dict | None:
    t = _current(t, tid, r)
    who = r["reviewer"]
    if who not in reviewer_kinds:
        raise Illegal(f"{who} is not a reviewer")
    earlier = t["reviews"].get(who)
    if earlier:
        if earlier["approve"] == r["approve"]:
            return None
        raise Illegal(f"{who} already reviewed this version")
    return decide({**t, "reviews": {**t["reviews"], who: r}}, verifiers, reviewer_kinds, policy, max_attempts)


def decide(t: dict, verifiers: list[str], reviewer_kinds: dict, policy: dict, max_attempts: int) -> dict:
    by = {o: {v for v, a in t["attestations"].items() if a["outcome"] == o} for o in ("pass", "fail", "flaky")}
    undecided = set(verifiers) - set(t["attestations"])
    passes = E.certifies(by["pass"], policy)
    if E.certifies(by["flaky"], policy) or (passes and E.certifies(by["fail"] | by["flaky"], policy)):
        return {**t, "state": NEEDS_HUMAN, "reason": "the tests are nondeterministic: verifiers who ran the same "
                f"tests on the same version disagree (pass {sorted(by['pass'])}, fail {sorted(by['fail'])}, "
                f"flaky {sorted(by['flaky'])})"}
    if E.certifies(by["fail"], policy):
        return _reopen(t, f"tests fail (attested by {', '.join(sorted(by['fail']))})", max_attempts)
    if not passes:
        if not E.certifies(by["pass"] | undecided, policy):
            return {**t, "state": NEEDS_HUMAN, "reason": "the verifiers' outcomes certify nothing"}
        return t
    level = t["review"]
    approvals = []
    if level != "none":
        qualified = {r: v for r, v in t["reviews"].items() if E.qualifies(reviewer_kinds[r], level)}
        rejecters = sorted(r for r, v in qualified.items() if not v["approve"])
        if rejecters:
            return _reopen(t, f"rejected in review by {', '.join(rejecters)}", max_attempts)
        approvals = [v for r, v in sorted(qualified.items()) if v["approve"]]
        if not approvals:
            return t
    return {**t, "state": APPROVED, "certificate": {
        "attestations": [t["attestations"][v] for v in sorted(by["pass"])], "reviews": approvals[:1]}}


def needs_attestation(t: dict, verifier: str) -> bool:
    return t["state"] == UNDER_CHECK and verifier not in t["attestations"]


def needs_review(t: dict, reviewer: str) -> bool:
    return t["state"] == UNDER_CHECK and t["review"] != "none" and reviewer not in t["reviews"]


# --- the committer -----------------------------------------------------------------------------
def committing(t: dict) -> dict:
    if t["state"] != APPROVED:
        raise Illegal(f"cannot commit a task in state {t['state']}")
    return {**t, "state": COMMITTING}


def committed(t: dict) -> dict:
    if t["state"] != COMMITTING:
        raise Illegal(f"task is {t['state']}, not COMMITTING")
    return {**t, "state": COMMITTED}


def commit_refused(t: dict, reason: str, max_attempts: int) -> dict:
    if t["state"] != COMMITTING:
        raise Illegal(f"task is {t['state']}, not COMMITTING")
    if reason.startswith("stale base"):
        return _reopen(t, reason, max_attempts)      # the code moved on: do the work again on top of it
    return {**t, "state": REFUSED, "reason": reason}


# --- what requesters get back -------------------------------------------------------------------
def grant(tid: str, t: dict, lease_s: float) -> dict:
    return {"task": tid, "token": t["epoch"], "signed_task": t["task"], "attempt": t["attempts"], "lease_s": lease_s}


def check_order(tid: str, t: dict) -> dict:
    return {"task": tid, "signed_task": t["task"], "base": t["base"], "result": t["result"],
            "round": t["submitted_token"], "changed": t["changed"]}


def commit_order(tid: str, t: dict) -> dict:
    return {"task": tid, "signed_task": t["task"], "base": t["base"], "result": t["result"],
            "attestations": t["certificate"]["attestations"], "reviews": t["certificate"]["reviews"]}
