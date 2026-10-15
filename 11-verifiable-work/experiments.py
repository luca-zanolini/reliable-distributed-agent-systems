"""The experiments: what the design says must happen, then what really happens.

Usage: python experiments.py [--only N ...]

Each experiment starts a fresh system (three etcd members, two front-ends, the repository with
the starting code, workers, three verifiers that really run the tests, reviewers, the committer,
the issuer), makes one part behave badly or one rule differ, and checks:

  the guarantee   every version that became the current code carries evidence that verifies
                  against the repository's pinned trust (re-checked here, independently of the
                  repository's code), touched no protected path, and was built on the code that
                  was current when it was committed
  the scenario    the bad behaviour really happened, and was stopped (or, in a counterfactual,
                  was not) where the design says

Ground truth about the code comes from tests no worker has seen (project.HIDDEN). The
predictions are design decision D7 (15 Oct 2026); 5b, 7b and 7c are counterfactuals.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter

import evidence as E
import project
from lab import Lab

EXPECTED = {
    1: "a worker says 'all tests pass' and asks the repository to commit on its own signed word: refused",
    2: "a worker fixes the bug but also weakens an acceptance test: the pinned tests pass, the repository refuses the change",
    3: "a worker claims a pass; the verifiers' own runs fail: not approved",
    4: "one verifier signs 'pass' on failing code (f = 1, 3 verifiers): not committed",
    5: "two certified changes built on the same code: the second is refused (stale base), redone on the new code, committed",
    "5b": "(counterfactual) the repository does not check the base: the second change silently erases the first",
    6: "a flaky test: the verifiers' runs disagree: nothing is committed, the task goes to a human",
    7: "a fix special-cased to the test's input, on fencing.py, a careful (human) reviewer required: rejected, not committed",
    "7b": "(counterfactual) the same, with a tests-only rule for fencing.py: committed",
    "7c": "(counterfactual) the same, with a bot reviewer allowed, and a comment telling reviewers to approve: committed",
    "7d": "the same, with a human reviewer required: the bot's approval does not count, the human rejects: not committed",
    8: "a changelog change: committed on the tests alone, no reviewer asked",
    "8b": "a changelog task that also quietly changes fencing.py, through a front-end that skips reviews: the repository refuses",
}
COUNTERFACTUAL = {"5b", "7b", "7c"}


def guarantee(lab, chain=True) -> tuple[bool, str]:
    commits = lab.records("commits.jsonl")
    bad, protected, off_head = [], [], []
    for c in commits:
        changed = E.changed_paths(lab.version(c["base"]), lab.version(c["result"]))
        if E.check_commit(lab.pinned, c["signed_task"], c["base"], c["result"], changed, c["attestations"], c["reviews"]):
            bad.append(c["task"])
        if any(p.startswith("tests/") for p in changed):
            protected.append(c["task"])
        if c["base"] != c["previous_head"]:
            off_head.append(c["task"])
    ok = not bad and not protected and (not off_head or not chain)
    return ok, (f"{len(commits)} commit(s), each with evidence that verifies, no protected path, each on the then-current code"
                if ok else f"UNVERIFIED {bad}, protected {protected}, not on the current code {off_head}")


def finish(lab, timeout_s=90.0) -> dict:
    lab.wait(lambda: (s := lab.status()) and s["all_final"], timeout_s, "every task final")
    lab.wait(lambda: lab.procs["k1"].poll() is not None, 30, "the committer to finish")
    return lab.status()


def states(s) -> dict:
    return dict(sorted(Counter(v["state"] for v in s["tasks"].values()).items()))


def outcomes(lab, who_prefix="v") -> list[dict]:
    return [e for w in lab.ids if w.startswith(who_prefix) for e in lab.journal(w) if e.get("path") == "/attest"]


def head_files(lab) -> dict:
    return lab.version(lab.head())


def t2_passes(files) -> bool:
    return project.run_tests(files, {"tests/test_grant.py": project.TEST_GRANT})[0][0]


# --- the experiments --------------------------------------------------------------------------------
def _claim_pass_lab(**kw):
    return Lab(tasks=("T1", "T3"), workers=(("w1", "claim-pass"), ("w2", "")), **kw)


def exp1():
    lab = _claim_pass_lab()
    try:
        s = finish(lab)
        direct = [e for e in lab.journal("w1") if e.get("state") == "direct_commit"]
        refused = [e["outcome"].get("refused") for e in direct if e["outcome"].get("refused")]
        mech = bool(direct) and len(refused) == len(direct) and "w1: not a pinned verifier" in refused[0]
        return mech, (f"w1 asked the repository directly {len(direct)} time(s), refused each time: "
                      f"{refused[0][:90] if refused else None}; tasks {states(s)}"), lab
    finally:
        lab.close()


def exp2():
    lab = Lab(tasks=("T1",), workers=(("w1", "edit-tests"),))
    try:
        s = finish(lab)
        refusals = lab.records("refusals.jsonl")
        passes = [e for e in outcomes(lab) if e.get("outcome") == "pass"]
        mech = bool(passes) and bool(refusals) and all("protected path changed: tests/test_check.py" in r["reason"]
                                                       for r in refusals) and not lab.records("commits.jsonl")
        return mech, (f"pinned tests passed ({len(passes)} attestations); repository refused: "
                      f"{refusals[0]['reason'] if refusals else None}; T1 {s['tasks']['T1']['state']}"), lab
    finally:
        lab.close()


def exp3():
    lab = _claim_pass_lab()
    try:
        s = finish(lab)
        w1_results = {e["result"] for e in lab.journal("w1") if e.get("path") == "/submit" and e.get("mode") == "claim-pass"}
        fails = [e for e in outcomes(lab) if e["result"] in w1_results and e["outcome"] == "fail"]
        committed = {c["result"] for c in lab.records("commits.jsonl")}
        mech = bool(w1_results) and len(fails) >= 2 and not (w1_results & committed)
        return mech, (f"w1 claimed a pass on {len(w1_results)} version(s); the verifiers' runs failed "
                      f"({len(fails)} fail attestations); none committed; tasks {states(s)}"), lab
    finally:
        lab.close()


def exp4():
    lab = _claim_pass_lab(verifiers=(("v1", "pass-all"), ("v2", ""), ("v3", "")))
    try:
        s = finish(lab)
        w1_results = {e["result"] for e in lab.journal("w1") if e.get("path") == "/submit" and e.get("mode") == "claim-pass"}
        lie = [e for e in lab.journal("v1") if e.get("path") == "/attest" and e["result"] in w1_results and e["outcome"] == "pass"]
        committed = {c["result"] for c in lab.records("commits.jsonl")}
        mech = bool(lie) and not (w1_results & committed)
        return mech, f"v1 signed 'pass' on {len(lie)} failing version(s); none committed; tasks {states(s)}", lab
    finally:
        lab.close()


def exp5(no_base_check=False):
    lab = Lab(tasks=("T1", "T2"), repo_flags=["--no-base-check"] if no_base_check else [])
    try:
        s = finish(lab)
        stale = [r for r in lab.records("refusals.jsonl") if r["reason"].startswith("stale base")]
        files = head_files(lab)
        both = project.hidden_check_passes(files) and t2_passes(files)
        commits = lab.records("commits.jsonl")
        if no_base_check:
            same_base = len({c["base"] for c in commits}) == 1 and len(commits) == 2
            return (same_base and not both), (f"both committed on the same base: {same_base}; the current code "
                                               f"has both fixes: {both}; tasks {states(s)}"), lab
        mech = bool(stale) and both and all(v["state"] == "COMMITTED" for v in s["tasks"].values())
        return mech, (f"refused as stale: {len(stale)}; redone and committed; the current code has both fixes: "
                      f"{both}; tasks {states(s)}"), lab
    finally:
        lab.close()


def exp6():
    lab = Lab(tasks=("T4",), workers=(("w1", ""),), runs=5)
    try:
        s = finish(lab)
        t = s["tasks"]["T4"]
        mech = t["state"] == "NEEDS_HUMAN" and "nondeterministic" in (t["reason"] or "") and not lab.records("commits.jsonl")
        return mech, f"T4 {t['state']}: attestations {t['attestations']}; commits: {len(lab.records('commits.jsonl'))}", lab
    finally:
        lab.close()


def _cheat(fencing_rule, reviewers):
    return Lab(tasks=("T1",), workers=(("w1", "cheat"),), fencing_rule=fencing_rule, reviewers=reviewers)


def exp7(rule="any", reviewers=(("r1", "human"),), counterfactual=False):
    lab = _cheat(rule, reviewers)
    try:
        s = finish(lab)
        cheat_passes = [e for e in outcomes(lab) if e["outcome"] == "pass"]
        verdicts = {r: [e["approve"] for e in lab.journal(r) if e.get("path") == "/review"] for r, _ in reviewers}
        cheat_in = not project.hidden_check_passes(head_files(lab)) and head_files(lab) != project.GENESIS
        if counterfactual:
            return cheat_in, (f"pinned tests passed on the special-cased fix ({len(cheat_passes)} attestations); "
                              f"reviews {verdicts}; the cheat is now the current code: {cheat_in}"), lab
        mech = bool(cheat_passes) and not cheat_in and not lab.records("commits.jsonl")
        return mech, (f"pinned tests passed on the special-cased fix ({len(cheat_passes)} attestations); reviews "
                      f"{verdicts}; committed: {len(lab.records('commits.jsonl'))}; T1 {s['tasks']['T1']['state']}"), lab
    finally:
        lab.close()


def exp8():
    lab = Lab(tasks=("T3",), workers=(("w1", ""),))
    try:
        s = finish(lab)
        asked = [e for e in lab.journal("r1") if e.get("path") == "/review"]
        commits = lab.records("commits.jsonl")
        mech = len(commits) == 1 and not asked and commits[0]["reviews"] == []
        return mech, (f"committed {len(commits)}, with {len(commits[0]['attestations']) if commits else 0} attestations "
                      f"and no review; the reviewer was asked {len(asked)} time(s); T3 {s['tasks']['T3']['state']}"), lab
    finally:
        lab.close()


def exp8b():
    lab = Lab(tasks=("T3",), workers=(("w1", "sneak"),), frontend_flags={"f1": ["--byzantine", "skip-review"]},
              worker_urls="f1")
    try:
        s = finish(lab)
        refusals = lab.records("refusals.jsonl")
        mech = (bool(refusals) and "fencing.py" in refusals[0]["reason"] and "no valid approval" in refusals[0]["reason"]
                and lab.head() == lab.genesis and lab.counters("f1").get("reviews_skipped", 0) > 0)
        return mech, (f"f1 skipped {lab.counters('f1').get('reviews_skipped')} review(s); repository refused: "
                      f"{refusals[0]['reason'] if refusals else None}; the code is unchanged: {lab.head() == lab.genesis}; "
                      f"T3 {s['tasks']['T3']['state']}"), lab
    finally:
        lab.close()


RUN = {1: exp1, 2: exp2, 3: exp3, 4: exp4, 5: exp5, "5b": lambda: exp5(True), 6: exp6, 7: exp7,
       "7b": lambda: exp7("none", (("r1", "human"),), True), "7c": lambda: exp7("any", (("b1", "bot"),), True),
       "7d": lambda: exp7("human", (("b1", "bot"), ("r1", "human"))), 8: exp8, "8b": exp8b}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    a = ap.parse_args()
    names = list(RUN) if not a.only else [int(x) if x.isdigit() else x for x in a.only]
    results = {}
    for n in names:
        t0 = time.time()
        mech, observed, lab = RUN[n]()
        ok, inv = guarantee(lab)
        results[n] = (mech, ok)
        print(f"\n[{n}] ({time.time() - t0:.0f} s)\n   expected: {EXPECTED[n]}\n   observed: {observed}\n"
              f"   scenario {'CONFIRMED' if mech else 'NOT CONFIRMED'}; guarantee {'HOLDS' if ok else 'BROKEN'}: {inv}",
              flush=True)

    def verdict(n, m, g):
        if n in COUNTERFACTUAL:
            return "counterfactual: got through, as predicted" if m else "COUNTERFACTUAL NOT REPRODUCED"
        return "ok" if m and g else "FAILED"
    print("\nsummary:", {n: verdict(n, m, g) for n, (m, g) in results.items()})


if __name__ == "__main__":
    main()
