"""The failure experiments: what the design says must happen, then what really happens.

Usage: python experiments.py [--backend proc|container] [--only N ...]

Each experiment starts a fresh system (three etcd members, two front-ends, 3 workers,
2 checkers, a sender, the mail service; 8 tasks, the last one with an invalid address),
injects one failure, waits until every task is final, and checks:

  the invariant   every SENT task was delivered exactly once, no FAILED task was delivered
                  (the mail service's own record is the ground truth), and no token was
                  ever given to two holders: every (task, token) a participant was told it
                  holds names exactly one participant
  the scenario    the specific mechanism the expectation relies on was really exercised

The expectations follow from the design decisions D1-D6 (11 Oct 2026); experiment 8 is
the counterfactual that shows what the conditional writes are for.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from lab import Lab as _Lab
from wire import VERSION, Claim, Client, RPCError

SCALE = {"proc": 1.0, "container": 2.0}


def Lab(backend, work_s=0.6, lease_s=3.0, **kw):
    k = SCALE[backend]
    return _Lab(backend, work_s=work_s * k, lease_s=lease_s * k, **kw)


EXPECTED = {
    1: "a new etcd leader within seconds; nothing committed is lost; no task assigned twice",
    2: "without a majority no token is issued (503: nothing decided); work resumes on thaw",
    3: "the claim was committed but never answered; the retry, through the other front-end, "
       "carries the same request id and gets the same task and token",
    4: "two front-ends racing: each task granted once; the losers' conditions fail and they re-read",
    5: "the frozen holder's lease runs out in etcd, the task is reopened; on thaw it is fenced",
    6: "a dead worker's task is reopened after its lease even though the etcd leader changed in between",
    7: "the SENDING intent survives the leader's crash; the email goes out exactly once",
    8: "(counterfactual) with blind writes, racing claims give the same token to two holders",
}


def unique_grants(lab) -> tuple[bool, str]:
    holders = defaultdict(set)
    for task, token, who in lab.grants():
        holders[(task, token)].add(who)
    shared = {k: sorted(v) for k, v in holders.items() if len(v) > 1}
    return not shared, f"{len(holders)} grants, shared {shared}" if shared else f"{len(holders)} grants, none shared"


def invariant(lab) -> tuple[bool, str]:
    s, m = lab.status(), lab.mail()
    sent = {t for t, v in s["tasks"].items() if v["state"] == "SENT"}
    failed = {t for t, v in s["tasks"].items() if v["state"] == "FAILED"}
    counts = Counter(m["deliveries"])
    ok = set(counts) == sent and all(n == 1 for n in counts.values()) and not failed & set(counts)
    unique, grants = unique_grants(lab)
    return ok and unique, f"{len(sent)} sent, {len(failed)} failed, deliveries {dict(sorted(counts.items()))}; {grants}"


def finish(lab, timeout_s=120.0) -> dict:
    lab.wait(lambda: (s := lab.status()) and s["all_final"], timeout_s, "every task final")
    lab.wait(lambda: lab.exited("s1"), 60, "the sender to finish")
    return lab.status()


def holding(lab, who: str, state: str):
    s = lab.status()
    return next(((t, v["epoch"]) for t, v in s["tasks"].items()
                 if v["owner"] == who and v["state"] == state), None) if s else None


def freeze_while_holding(lab, who: str, state: str) -> tuple[str, int]:
    for _ in range(40):
        lab.wait(lambda: holding(lab, who, state), 60, f"{who} to hold a task in {state}")
        lab.freeze(who)
        time.sleep(0.2)
        held = holding(lab, who, state)
        if held:
            return held
        lab.thaw(who)
    raise TimeoutError(f"could not catch {who} holding a task")


def probe_client(lab, who: str, front_end: str, timeout_s: float, attempts: int = 1) -> Client:
    return Client(lab.fe_url(front_end), who, lab.registry[who]["secret"], timeout_s=timeout_s, attempts=attempts)


# --- the experiments ------------------------------------------------------------------
def exp1(backend):
    lab = Lab(backend)
    try:
        lab.wait(lambda: len(lab.grants()) >= 3, 60, "work under way")
        leader = lab.etcd.leader()
        t0 = time.time()
        lab.etcd.kill(leader)
        new = lab.etcd.wait_leader(30)
        took = time.time() - t0
        finish(lab)
        ok, inv = invariant(lab)
        return (f"killed etcd leader {leader} mid-run; {new} led after {took:.1f} s", new != leader, ok, inv)
    finally:
        lab.close()


def exp2(backend):
    lab = Lab(backend, probes=1)
    try:
        lab.wait(lambda: len(lab.grants()) >= 3, 60, "work under way")
        leader = lab.etcd.leader()
        followers = [m for m in ("e1", "e2", "e3") if m != leader]
        before = len(lab.grants())
        for m in followers:
            lab.etcd.freeze(m)
        frozen_at = time.time()
        try:
            out = probe_client(lab, "p1", "f1", timeout_s=15).call("/claim", Claim(v=VERSION, who="p1", request_id="probe-during-freeze"))
            probe = f"GRANTED {out}" if out.get("task") else f"answered {out}"
        except RPCError as e:
            probe = f"refused {e.status}"
        except OSError as e:
            probe = f"no decision ({type(e).__name__}: {str(e)[:60]})"
        time.sleep(max(0.0, 6.0 - (time.time() - frozen_at)))
        during = [g for g in lab.grants()[before:]]
        for m in followers:
            lab.etcd.thaw(m)
        thawed = time.time()
        finish(lab)
        resumed = time.time() - thawed
        ok, inv = invariant(lab)
        mechanism = "GRANTED" not in probe
        return (f"froze the 2 followers of {leader} for {thawed - frozen_at:.1f} s; probe claim during the freeze: "
                f"{probe}; all work done {resumed:.1f} s after the thaw", mechanism, ok, inv)
    finally:
        lab.close()


def exp3(backend):
    # Both front-ends carry the same once-only switch: whichever commits a claim first
    # exits before replying, and the other one never does (the flag file exists by then).
    flag = Path(tempfile.mkdtemp(prefix="exp3-")) / "crashed.json"
    lab = Lab(backend, frontend_flags=["--crash-after-claim-once", str(flag)])
    try:
        lab.wait(lambda: flag.exists(), 60, "a front-end to crash after a claim")
        crashed = json.loads(flag.read_text())
        lab.start_frontend(crashed["front_end"])        # back on the same port (the switch is spent)
        finish(lab)
        tid = crashed["task"]
        grants_of_task = [(t, tok, w) for t, tok, w in lab.grants() if t == tid]
        replayed = lab.counters().get("claim_replayed", 0)
        ok, inv = invariant(lab)
        first = grants_of_task[0] if grants_of_task else None
        return (f"{crashed['front_end']} crashed after committing a claim on {tid}, before replying; "
                f"first grant of {tid} journaled by a participant: {first}; claims answered again from "
                f"the record: {replayed}", replayed >= 1 and first is not None, ok, inv)
    finally:
        lab.close()


def race(backend, blind: bool):
    flags = ["--blind-writes"] if blind else []
    lab = Lab(backend, tasks=3, frontend_flags=flags, agents=False, probes=6)
    try:
        barrier = threading.Barrier(6)
        got = {}

        def claimer(i):
            who = f"p{i}"
            c = probe_client(lab, who, "f1" if i % 2 else "f2", timeout_s=10)
            barrier.wait()
            try:
                got[who] = c.call("/claim", Claim(v=VERSION, who=who, request_id=f"race-{who}-0001"))
            except (RPCError, OSError) as e:
                got[who] = {"error": str(e)}
        threads = [threading.Thread(target=claimer, args=(i,)) for i in range(1, 7)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        grants = defaultdict(list)
        for who, g in got.items():
            if g.get("task"):
                grants[(g["task"], g["token"])].append(who)
        shared = {k: v for k, v in grants.items() if len(v) > 1}
        conflicts = lab.counters().get("conflicts", 0)
        return grants, shared, conflicts
    finally:
        lab.close()


def exp4(backend):
    grants, shared, conflicts = race(backend, blind=False)
    tasks = sorted(t for t, _ in grants)
    ok = not shared and len(tasks) == len(set(tasks)) == 3
    return (f"6 simultaneous claims through 2 front-ends for 3 tasks: granted {dict(grants)}; "
            f"conditions that failed and were re-read: {conflicts}", conflicts >= 1, ok,
            "each task granted once" if ok else f"shared {shared}")


def exp5(backend):
    lab = Lab(backend)
    try:
        tid, epoch = freeze_while_holding(lab, "w1", "IN_PROGRESS")
        lab.wait(lambda: lab.status()["tasks"][tid]["epoch"] > epoch, 60, f"{tid} to move on")
        lab.thaw("w1")
        finish(lab)
        fenced = [e for e in lab.journal("w1") if e.get("task") == tid and e.get("token") == epoch
                  and e.get("state") == "fenced"]
        ok, inv = invariant(lab)
        return (f"froze w1 holding {tid} (token {epoch}); the task moved on; after the thaw w1 was "
                f"{'fenced' if fenced else 'NOT fenced'}", bool(fenced), ok, inv)
    finally:
        lab.close()


def exp6(backend):
    lab = Lab(backend)
    try:
        lab.wait(lambda: holding(lab, "w2", "IN_PROGRESS"), 60, "w2 to hold a task")
        tid, epoch = holding(lab, "w2", "IN_PROGRESS")
        lab.kill("w2")
        leader = lab.etcd.leader()
        lab.etcd.kill(leader)
        new = lab.etcd.wait_leader(30)
        finish(lab)
        later = [(t, tok, w) for t, tok, w in lab.grants() if t == tid and tok > epoch]
        expired = lab.counters().get("expired", 0)
        ok, inv = invariant(lab)
        return (f"killed w2 holding {tid} (token {epoch}), then the etcd leader {leader} ({new} took over); "
                f"{tid} was reopened ({expired} expiries) and later granted as {later[:1]}",
                bool(later) and expired >= 1, ok, inv)
    finally:
        lab.close()


def exp7(backend):
    k = SCALE[backend]
    lab = Lab(backend, mail_flags=["--slow-first-reply", str(4 * k)])
    try:
        lab.wait(lambda: any(v["state"] == "SENDING" for v in lab.status()["tasks"].values()), 120, "a send in flight")
        tid = next(t for t, v in lab.status()["tasks"].items() if v["state"] == "SENDING")
        leader = lab.etcd.leader()
        lab.etcd.kill(leader)
        new = lab.etcd.wait_leader(30)
        finish(lab)
        state = lab.status()["tasks"][tid]["state"]
        ok, inv = invariant(lab)
        return (f"killed etcd leader {leader} while {tid} was SENDING ({new} took over); {tid} ended {state}",
                state == "SENT", ok, inv)
    finally:
        lab.close()


def exp8(backend):
    grants, shared, _ = race(backend, blind=True)
    return (f"blind writes, 6 simultaneous claims for 3 tasks: granted {dict(grants)}",
            bool(shared), not shared, f"shared tokens: {shared}" if shared else "none shared")


EXPERIMENTS = {1: exp1, 2: exp2, 3: exp3, 4: exp4, 5: exp5, 6: exp6, 7: exp7, 8: exp8}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="proc", choices=["proc", "container"])
    ap.add_argument("--only", type=int, nargs="*")
    a = ap.parse_args()
    for n in a.only or sorted(EXPERIMENTS):
        t0 = time.time()
        observed, mechanism, holds, detail = EXPERIMENTS[n](a.backend)
        print(f"\n[{n}] ({time.time() - t0:.0f} s)")
        print(f"   expected: {EXPECTED[n]}")
        print(f"   observed: {observed}")
        verdict = "HOLDS" if holds else "BROKEN"
        if n == 8:
            print(f"   mechanism {'CONFIRMED' if mechanism else 'NOT SEEN'}: {detail}")
        else:
            print(f"   mechanism {'CONFIRMED' if mechanism else 'NOT SEEN'}; invariant {verdict}: {detail}")


if __name__ == "__main__":
    main()
