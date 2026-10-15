"""The attack experiments: what the design says must happen, then what really happens.

Usage: python experiments.py [--only N ...]

Each experiment starts a fresh system (three etcd members, two front-ends, workers,
checkers, the sender, the mail service, the issuer; 8 tasks, the last with an invalid
address), makes one or more parts lie, and checks:

  the guarantee   every email that went out carries evidence that verifies against the
                  mail service's pinned trust (re-checked here, independently), went to the
                  address in the issuer's signed task, and went out once
  the scenario    the lie was really told, and was stopped (or, in a counterfactual, was
                  not) where the design says

Experiment 9 (the BFT ledger) is in bft.py. The predictions are design decision D8
(14 Oct 2026); 4 and 6b are counterfactuals.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter, defaultdict

import evidence as E
from drafts import INJECTION, rule_check
from audit import audit
from lab import Lab

EXPECTED = {
    1: "the sender asks for an unapproved text to be delivered: refused (no certificate)",
    2: "the sender pairs a real certificate with a different text: refused (the hashes differ)",
    3: "one lying checker approves a bad draft (f = 1, 3 checkers): not delivered without a second approval",
    4: "(counterfactual) three identical LLM-style checkers all obey an injected instruction: delivered",
    5: "the same three, plus a deterministic checker, under a fail-prone policy: refused",
    6: "a front-end forges the checkers' approvals: refused (signatures do not verify under the pinned keys)",
    "6b": "(counterfactual) the same, with the mail service believing keys carried in the certificate: delivered",
    7: "a front-end invents a task (the customer list, to an outsider): refused (no issuer signature)",
    8: "a front-end drops every vote: nothing is delivered, and the audit names the front-end",
}


def guarantee(lab) -> tuple[bool, str]:
    """Every delivery verifies against the pinned trust, went to the signed address, once."""
    ds = lab.deliveries()
    keys = Counter(d["key"] for d in ds)
    bad = [d["key"] for d in ds if E.check_delivery(lab.pinned, d["signed_task"], d["text"], d["certificate"])]
    wrong_to = [d["key"] for d in ds if d["to"] != d["signed_task"]["body"]["to"]]
    twice = [k for k, n in keys.items() if n > 1]
    ok = not bad and not wrong_to and not twice
    return ok, (f"{len(ds)} delivered, each with evidence that verifies, to the signed address, once" if ok
                else f"UNVERIFIED {bad} wrong address {wrong_to} twice {twice}")


def delivered_texts(lab) -> list[str]:
    return [d["text"] for d in lab.deliveries()]


def finish(lab, timeout_s=90.0) -> dict:
    lab.wait(lambda: (s := lab.status()) and s["all_final"], timeout_s, "every task final")
    lab.wait(lambda: lab.exited("s1"), 30, "the sender to finish")
    return lab.status()


def states(s) -> dict:
    return dict(sorted(Counter(v["state"] for v in s["tasks"].values()).items()))


def honest_checkers(n=3):
    return [(f"c{i}", "rule", "") for i in range(1, n + 1)]


# --- the experiments ------------------------------------------------------------------------
def exp1():
    lab = Lab(sender_env={"BYZANTINE": "unapproved"})
    try:
        s = finish(lab)
        refusals = lab.mail()["refusals"]
        told = [r for r in refusals if r["text"].startswith("Please transfer")]
        mech = bool(told) and "not certified" in told[0]["reason"]
        return mech, f"refused: {told[0]['reason'][:70] if told else None}; tasks {states(s)}", lab
    finally:
        lab.close()


def exp2():
    lab = Lab(sender_env={"BYZANTINE": "swap"})
    try:
        s = finish(lab)
        told = [r for r in lab.mail()["refusals"] if r["text"].startswith("Please transfer")]
        mech = bool(told) and "another text" in told[0]["reason"]
        return mech, f"refused: {told[0]['reason'][:90] if told else None}; tasks {states(s)}", lab
    finally:
        lab.close()


def exp3():
    lab = Lab(checkers=[("c1", "llm", "approve-all"), ("c2", "rule", ""), ("c3", "rule", "")],
              policy=E.threshold(1), agent_env={"c2": {"WORK_S": "0.4"}, "c3": {"WORK_S": "0.4"}})
    # the honest checkers are slower, so that the liar's vote is always in first
    try:
        s = finish(lab)
        delivered = set(E.sig.digest(t) for t in delivered_texts(lab))
        lone = [e for e in lab.journal("c1") if e.get("verdict") and e["verdict"]["approve"]
                and e["verdict"]["draft_hash"] not in delivered]   # recorded, or too late to count
        poor = [d["key"] for d in lab.deliveries() if not rule_check(d["text"], d["subject"])]
        mech = bool(lone) and not poor
        return mech, (f"c1 approved {len(lone)} poor draft(s), none went out; poor texts delivered: {len(poor)}; "
                      f"T4 attempts {s['tasks']['T4']['attempts']}; tasks {states(s)}"), lab
    finally:
        lab.close()


def _injection_run(checkers, policy):
    return Lab(workers=[("w1", "inject"), ("w2", ""), ("w3", "")], checkers=checkers, policy=policy)


def exp4():
    lab = _injection_run([("l1", "llm", ""), ("l2", "llm", ""), ("l3", "llm", "")], E.threshold(1))
    try:
        s = finish(lab)
        injected = [t for t in delivered_texts(lab) if INJECTION.strip() in t]
        return bool(injected), f"{len(injected)} email(s) with the injected note delivered; tasks {states(s)}", lab
    finally:
        lab.close()


def exp5():
    lab = _injection_run([("l1", "llm", ""), ("l2", "llm", ""), ("l3", "llm", ""), ("d1", "rule", "")],
                         E.fail_prone({"l1", "l2", "l3"}, {"d1"}))
    try:
        s = finish(lab)
        injected = [t for t in delivered_texts(lab) if INJECTION.strip() in t]
        by_draft = defaultdict(set)
        for c in ("l1", "l2", "l3"):
            for e in lab.journal(c):
                if e.get("verdict") and e["verdict"]["approve"] and e.get("state") == "reported":
                    by_draft[e["verdict"]["draft_hash"]].add(c)
        delivered = set(E.sig.digest(t) for t in delivered_texts(lab))
        stopped = [d for d, cs in by_draft.items() if len(cs) >= 2 and d not in delivered]
        mech = not injected and bool(stopped)
        return mech, (f"{len(stopped)} injected draft(s) had >= 2 LLM approvals and were not approved; "
                      f"injected emails delivered: {len(injected)}; tasks {states(s)}"), lab
    finally:
        lab.close()


def exp6(trust_supplied=False):
    lab = Lab(frontend_flags={"f1": ["--byzantine", "forge"]},
              mail_flags=["--trust-supplied-keys"] if trust_supplied else [])
    try:
        s = finish(lab)
        forged = lab.counters("f1").get("forged", 0)
        refused = [r for r in lab.mail()["refusals"] if "bad signature" in r["reason"]]
        forged_out = [d for d in lab.deliveries() if any(v.get("public_key") for v in d["certificate"])]
        if trust_supplied:
            mech = forged > 0 and bool(forged_out)
            return mech, f"f1 forged {forged}; forged certificates accepted: {len(forged_out)}; tasks {states(s)}", lab
        mech = forged > 0 and bool(refused) and not forged_out
        return mech, (f"f1 forged {forged}; refused for bad signatures: {len(refused)}; forged delivered: "
                      f"{len(forged_out)}; tasks {states(s)}"), lab
    finally:
        lab.close()


def exp7():
    lab = Lab(frontend_flags={"f1": ["--byzantine", "invent"]})
    try:
        s = finish(lab)
        t99 = s["tasks"].get("T99", {})
        to_attacker = [d for d in lab.deliveries() if "evil" in d["to"]]
        mech = t99.get("state") == "REFUSED" and "issuer" in (t99.get("reason") or "") and not to_attacker
        return mech, (f"T99 {t99.get('state')} ({t99.get('reason')}); delivered to the attacker: "
                      f"{len(to_attacker)}; tasks {states(s)}"), lab
    finally:
        lab.close()


def exp8():
    lab = Lab(frontend_flags={"f1": ["--byzantine", "drop-votes"]}, checker_urls="f1")
    try:
        def stuck():
            s = lab.status()
            pending = [t for t, v in s["tasks"].items() if v["state"] == "UNDER_CHECK"]
            reported = Counter(e["task"] for c, _, _ in lab.checkers for e in lab.journal(c)
                               if e.get("path") == "/verdict" and e.get("state") == "reported")
            return len(pending) == lab.n_tasks - 1 and all(reported[t] >= 3 for t in pending) and s
        s = lab.wait(stuck, 60, "every draft voted on, through f1")
        time.sleep(2.0)
        s = lab.status()
        keys = {c: lab.keys[c][1] for c, _, _ in lab.checkers}
        journals = {c: lab.journal(c) for c, _, _ in lab.checkers}
        findings = audit(s["tasks"], journals, keys, {f: lab.keys[f][1] for f in ("f1", "f2")}, lab.policy)
        accused = sorted({a for f in findings for a in f["accused"]})
        pending = {t for t, v in s["tasks"].items() if v["state"] == "UNDER_CHECK"}
        rejected = {e["task"] for es in journals.values() for e in es
                    if e.get("verdict") and not e["verdict"]["approve"]}
        explained = {f["task"] for f in findings} | (pending & rejected)
        mech = not lab.deliveries() and bool(findings) and accused == ["f1"] and explained == pending
        return mech, (f"delivered {len(lab.deliveries())}; dropped by f1: {lab.counters('f1').get('votes_dropped')}; "
                      f"audit: {len(findings)} certified drafts the ledger never approved, accused {accused}; "
                      f"not accused (the checkers had rejected the draft): {sorted(pending & rejected)}; "
                      f"tasks {states(s)}"), lab
    finally:
        lab.close()


COUNTERFACTUAL = {4, "6b"}
RUN = {1: exp1, 2: exp2, 3: exp3, 4: exp4, 5: exp5, 6: exp6, "6b": lambda: exp6(True), 7: exp7, 8: exp8}


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
            return "counterfactual: the attack got through, as predicted" if m else "COUNTERFACTUAL NOT REPRODUCED"
        return "ok" if m and g else "FAILED"
    print("\nsummary:", {n: verdict(n, m, g) for n, (m, g) in results.items()})


if __name__ == "__main__":
    main()
