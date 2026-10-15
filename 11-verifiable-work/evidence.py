"""What counts as evidence that a version may become the current code, and how it is checked.

Stage 10's evidence was a checker's signed opinion. Here most of it is a signed account of
something done: a verifier ran the issuer's tests on an exact version and reports the
outcome. Four signed documents:

  the task         {"kind": "task", "task", "body": {"title", "tests", "rules"}}
                   by the issuer. tests: the acceptance tests, path -> content hash,
                   pinned: the verifiers run these, whatever the worker's version contains.
                   rules: which paths need which review (below).
  an attestation   {"kind": "attestation", "task_hash", "base", "result", "tests", "env",
                   "outcome", "report"} by one verifier: "I ran these tests on version
                   `result` (built on `base`) in environment `env`; outcome pass / fail /
                   flaky; the output hashes to `report`."
  a review         {"kind": "review", "task_hash", "base", "result", "approve"} by one
                   reviewer, who read the change from `base` to `result` and judged it.
  versions         named by the hash of their contents, so naming one names its contents.

Rules, per path prefix (the most demanding rule among the changed paths applies; a path
no rule names needs a human):
  none        the tests are enough
  any         plus one approving review, by any pinned reviewer
  human       plus one approving review by a reviewer pinned as a human
  forbidden   the change may not touch this path at all (the acceptance tests live here)

check_commit is the whole of the repository's judgement. As in Stage 10, it trusts only
what it was installed with (pinned keys, kinds and the verifier policy) and what it computes
itself (which paths changed); it believes nothing the worker, the ledger or the committer says.
"""

from __future__ import annotations

from itertools import combinations

import sig

LEVELS = ["none", "any", "human"]


# --- documents ------------------------------------------------------------------------------
def task_doc(tid: str, body: dict) -> dict:
    return {"kind": "task", "task": tid, "body": body}


def issue(issuer_sk: str, tid: str, body: dict) -> dict:
    return {"task": tid, "body": body, "issuer_sig": sig.sign(issuer_sk, task_doc(tid, body))}


def task_hash(signed_task: dict) -> str:
    return sig.digest(sig.canon(task_doc(signed_task["task"], signed_task["body"])))


def version_hash(files: dict) -> str:
    return sig.digest(sig.canon(files))


def attestation_doc(t_hash, base, result, tests, env, outcome, report) -> dict:
    return {"kind": "attestation", "task_hash": t_hash, "base": base, "result": result, "tests": tests,
            "env": env, "outcome": outcome, "report": report}


def attest(verifier, sk, signed_task, base, result, env, outcome, report) -> dict:
    doc = attestation_doc(task_hash(signed_task), base, result, signed_task["body"]["tests"], env, outcome, report)
    return {**doc, "verifier": verifier, "sig": sig.sign(sk, doc)}


def review_doc(t_hash, base, result, approve) -> dict:
    return {"kind": "review", "task_hash": t_hash, "base": base, "result": result, "approve": approve}


def review(reviewer, sk, signed_task, base, result, approve) -> dict:
    doc = review_doc(task_hash(signed_task), base, result, approve)
    return {**doc, "reviewer": reviewer, "sig": sig.sign(sk, doc)}


def attestation_valid(a: dict, keys: dict[str, str]) -> bool:
    pk = keys.get(a.get("verifier"))
    doc = attestation_doc(a.get("task_hash"), a.get("base"), a.get("result"), a.get("tests"), a.get("env"),
                          a.get("outcome"), a.get("report"))
    return bool(pk) and sig.verify(pk, doc, a.get("sig", ""))


def review_valid(r: dict, keys: dict[str, str]) -> bool:
    pk = keys.get(r.get("reviewer"))
    doc = review_doc(r.get("task_hash"), r.get("base"), r.get("result"), r.get("approve"))
    return bool(pk) and sig.verify(pk, doc, r.get("sig", ""))


# --- policies -----------------------------------------------------------------------------------
def threshold(f: int) -> dict:
    return {"kind": "threshold", "f": f}


def fail_prone(*sets) -> dict:
    return {"kind": "fail_prone", "sets": [sorted(s) for s in sets]}


def certifies(signers: set[str], policy: dict) -> bool:
    if not signers:
        return False
    if policy["kind"] == "threshold":
        return len(signers) >= policy["f"] + 1
    return not any(signers <= set(s) for s in policy["sets"])


def as_fail_prone(members: list[str], f: int) -> dict:
    return fail_prone(*[set(c) for c in combinations(sorted(members), f)])


def rule_for(path: str, rules: list[dict]) -> str:
    """The rule of the longest matching prefix; no match means a human must look."""
    best = max((r for r in rules if path.startswith(r["prefix"])), key=lambda r: len(r["prefix"]), default=None)
    return best["review"] if best else "human"


def required_review(changed: list[str], rules: list[dict]) -> str:
    levels = [rule_for(p, rules) for p in changed]
    if "forbidden" in levels:
        return "forbidden"
    return max(levels, key=LEVELS.index, default="none")


def qualifies(kind: str, level: str) -> bool:
    return level == "any" or (level == "human" and kind == "human")


def changed_paths(before: dict, after: dict) -> list[str]:
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


# --- the repository's check ------------------------------------------------------------------------
def check_commit(pinned: dict, signed_task: dict, base: str, result: str, changed: list[str],
                 attestations: list[dict], reviews: list[dict]) -> str | None:
    """None if `result` may become the current code (on top of `base`), else why not.

    pinned: {"issuer": key, "verifiers": {id: key}, "policy": {...},
             "reviewers": {id: {"pk": key, "kind": "human" | "bot"}}}
    changed: the paths that differ between base and result, computed by the caller itself.
    """
    body = signed_task.get("body") or {}
    if not sig.verify(pinned["issuer"], task_doc(signed_task.get("task"), body), signed_task.get("issuer_sig", "")):
        return "task not signed by the issuer"
    level = required_review(changed, body.get("rules", []))
    if level == "forbidden":
        bad = [p for p in changed if rule_for(p, body["rules"]) == "forbidden"]
        return f"protected path changed: {', '.join(bad)}"
    t_hash = task_hash(signed_task)
    passed, problems = set(), []
    for a in attestations:
        who = a.get("verifier")
        if who not in pinned["verifiers"]:
            problems.append(f"{who}: not a pinned verifier")
        elif (a.get("task_hash"), a.get("base"), a.get("result")) != (t_hash, base, result):
            problems.append(f"{who}: attestation about another task or version")
        elif a.get("tests") != body.get("tests"):
            problems.append(f"{who}: ran other tests than the pinned ones")
        elif a.get("outcome") != "pass":
            problems.append(f"{who}: outcome {a.get('outcome')}")
        elif not attestation_valid(a, pinned["verifiers"]):
            problems.append(f"{who}: bad signature")
        else:
            passed.add(who)
    if not certifies(passed, pinned["policy"]):
        why = "; ".join(problems) if problems else "no attestations"
        return f"tests not certified: valid passes from {sorted(passed)} do not satisfy the policy ({why})"
    if level == "none":
        return None
    reviewer_keys = {r: e["pk"] for r, e in pinned["reviewers"].items()}
    for r in reviews:
        who = r.get("reviewer")
        entry = pinned["reviewers"].get(who)
        if (entry and qualifies(entry["kind"], level) and r.get("approve")
                and (r.get("task_hash"), r.get("base"), r.get("result")) == (t_hash, base, result)
                and review_valid(r, reviewer_keys)):
            return None
    who_needed = "a human reviewer" if level == "human" else "a reviewer"
    return f"review by {who_needed} required for {', '.join(changed)}: no valid approval"
