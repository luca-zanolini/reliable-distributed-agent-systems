"""What counts as evidence that an email may go out, and how it is checked.

Three signed documents make up the evidence for one email:

  the task       {"kind": "task", "task": id, "body": {"to", "subject"}}
                 signed by the issuer (the billing system): someone entitled to ask
                 for this email asked for it
  a verdict      {"kind": "verdict", "task_hash", "draft_hash", "approve"}
                 signed by one checker: this checker judged this exact text for this
                 exact task
  the text       the draft itself; its hash is what the verdicts are about

A certificate is a set of approving verdicts on the same (task, text). It is enough when
the checkers who signed it satisfy the approval policy:

  threshold f    at least f+1 distinct checkers: with at most f liars, one is honest
  fail-prone     a list of sets of checkers that may all fail together; the approvers
                 must not all lie inside one of them, so some approver is honest
                 whichever of those sets has failed

Both read "some honest checker approved this text". That is enough when honest checkers
cannot disagree (the check has one right answer): witness mode. The threshold rule is the
special case of the second whose fail-prone sets are all the sets of f checkers.

check_delivery is the whole of the mail service's judgement. It trusts only what it was
installed with (the pinned keys and policy) and what it can recompute (the hashes); it
believes nothing it is told by the sender or the ledger.
"""

from __future__ import annotations

from itertools import combinations

import sig


# --- documents -------------------------------------------------------------------------
def task_doc(tid: str, body: dict) -> dict:
    return {"kind": "task", "task": tid, "body": body}


def issue(issuer_sk: str, tid: str, body: dict) -> dict:
    """A task as the issuer publishes it: the document and the issuer's signature."""
    return {"task": tid, "body": body, "issuer_sig": sig.sign(issuer_sk, task_doc(tid, body))}


def task_hash(signed_task: dict) -> str:
    return sig.digest(sig.canon(task_doc(signed_task["task"], signed_task["body"])))


def verdict_doc(t_hash: str, d_hash: str, approve: bool) -> dict:
    return {"kind": "verdict", "task_hash": t_hash, "draft_hash": d_hash, "approve": approve}


def judge(checker: str, checker_sk: str, signed_task: dict, draft: str, approve: bool) -> dict:
    """A checker's signed verdict on one text for one task."""
    t_hash, d_hash = task_hash(signed_task), sig.digest(draft)
    return {"checker": checker, "task_hash": t_hash, "draft_hash": d_hash, "approve": approve,
            "sig": sig.sign(checker_sk, verdict_doc(t_hash, d_hash, approve))}


def verdict_valid(v: dict, keys: dict[str, str]) -> bool:
    pk = keys.get(v.get("checker"))
    return bool(pk) and sig.verify(pk, verdict_doc(v["task_hash"], v["draft_hash"], v["approve"]), v["sig"])


# --- the approval policy -----------------------------------------------------------------
def threshold(f: int) -> dict:
    return {"kind": "threshold", "f": f}


def fail_prone(*sets) -> dict:
    return {"kind": "fail_prone", "sets": [sorted(s) for s in sets]}


def certifies(approvers: set[str], policy: dict) -> bool:
    if not approvers:
        return False
    if policy["kind"] == "threshold":
        return len(approvers) >= policy["f"] + 1
    if policy["kind"] == "fail_prone":
        return not any(approvers <= set(s) for s in policy["sets"])
    raise ValueError(f"unknown policy {policy['kind']!r}")


def as_fail_prone(members: list[str], f: int) -> dict:
    """The threshold rule written as a fail-prone system: every set of f members."""
    return fail_prone(*[set(c) for c in combinations(sorted(members), f)])


# --- the mail service's check --------------------------------------------------------------
def check_delivery(pinned: dict, signed_task: dict, text: str, certificate: list[dict],
                   trust_supplied_keys: bool = False) -> str | None:
    """None if this text may be delivered for this task, else the reason it may not.

    pinned: {"issuer": public key, "checkers": {id: public key}, "policy": {...}},
    installed with the mail service, never received over the wire.
    trust_supplied_keys: the counterfactual. Believe a public key that a certificate
    entry carries for a checker the pinned list does not vouch for.
    """
    body = signed_task.get("body") or {}
    if not sig.verify(pinned["issuer"], task_doc(signed_task.get("task"), body), signed_task.get("issuer_sig", "")):
        return "task not signed by the issuer"
    t_hash, d_hash = task_hash(signed_task), sig.digest(text)
    approvers, problems = set(), []
    for v in certificate:
        who = v.get("checker")
        keys = dict(pinned["checkers"])
        if trust_supplied_keys and v.get("public_key"):
            keys[who] = v["public_key"]
        if who not in keys:
            problems.append(f"{who}: not a pinned checker")
        elif v.get("task_hash") != t_hash:
            problems.append(f"{who}: verdict about another task")
        elif v.get("draft_hash") != d_hash:
            problems.append(f"{who}: verdict about another text")
        elif not v.get("approve"):
            problems.append(f"{who}: rejected it")
        elif not verdict_valid(v, keys):
            problems.append(f"{who}: bad signature")
        else:
            approvers.add(who)
    if certifies(approvers, pinned["policy"]):
        return None
    why = "; ".join(problems) if problems else "no verdicts"
    return f"not certified: valid approvals from {sorted(approvers)} do not satisfy the policy ({why})"
