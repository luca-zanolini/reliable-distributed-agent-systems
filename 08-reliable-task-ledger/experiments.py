"""The failure experiments: each designer's prediction, then what really happens.

Usage: python experiments.py [--backend proc|container] [--only N ...]

Each experiment starts a fresh system (ledger, 3 workers, 2 checkers, a sender, the
mail service; 8 tasks, the last one with an invalid address), injects one failure,
waits until every task is final, and checks two things:

  the invariant  every SENT task was delivered exactly once, no FAILED task was
                 delivered at all (the mail service's own record is the ground truth)
  the scenario   the specific mechanism the prediction relied on was really used

Predictions are Luca's, written before any of this code existed (2 Oct 2026).
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter

from lab import Lab as _Lab

# Containers add latency to every command (starting, killing, freezing a VM), so in the
# container backend jobs and leases are stretched; the scenarios are otherwise identical.
SCALE = {"proc": 1.0, "container": 3.0}


def Lab(backend, work_s=0.4, lease_s=1.0, **kw):
    k = SCALE[backend]
    return _Lab(backend, work_s=work_s * k, lease_s=lease_s * k, **kw)

PREDICTIONS = {
    1: "lease runs out, the ledger reopens the task, another worker does it: done once, a bit later",
    2: "no longer valid: the token was increased and the task given to another worker",
    3: "an answer like 'OK, already accepted'",
    4: "fencing holds for the checker too",
    5: "(refined) the retried claim carries the same request id and gets the same task and token",
    6: "send again with the same idempotency key; not delivered twice; then the ledger records SENT",
    7: "unclear (outside server); send again, it will not be delivered twice",
    8: "when the ledger is back, leases are renewed (fresh duration)",
}


def invariant(lab: Lab) -> tuple[bool, str]:
    s, m = lab.status(), lab.mail()
    sent = {t for t, v in s["tasks"].items() if v["state"] == "SENT"}
    failed = {t for t, v in s["tasks"].items() if v["state"] == "FAILED"}
    counts = Counter(m["deliveries"])
    ok = set(counts) == sent and all(n == 1 for n in counts.values()) and not failed & set(counts)
    return ok, f"{len(sent)} sent, {len(failed)} failed, deliveries {dict(sorted(counts.items()))}"


def finish(lab: Lab, timeout_s=90.0) -> dict:
    lab.wait(lambda: (s := lab.status()) and s["all_final"], timeout_s, "every task final")
    lab.wait(lambda: lab.exited("s1"), 30, "the sender to finish")
    return lab.status()


def holding(lab, who: str, state: str):
    """(task, epoch) that `who` holds right now according to the ledger, or None."""
    s = lab.status()
    return next(((t, v["epoch"]) for t, v in s["tasks"].items()
                 if v["owner"] == who and v["state"] == state), None) if s else None


def freeze_while_holding(lab, who: str, state: str) -> tuple[str, int]:
    """Freeze `who` at a moment when the ledger says it holds a task in `state`. The
    ledger is asked again after the signal: the participant may have finished that task
    in the meantime (container commands take a while); if so, thaw and try again."""
    for _ in range(40):
        lab.wait(lambda: holding(lab, who, state), 60, f"{who} to hold a task in {state}")
        lab.freeze(who)
        time.sleep(0.2)
        held = holding(lab, who, state)
        if held:
            return held
        lab.thaw(who)
    raise TimeoutError(f"could not catch {who} holding a task")


def events(lab: Lab, task: str) -> list[str]:
    def show(e):
        who = f"{e['worker']} " if "worker" in e else ""
        return e["kind"] + (f"({who}t{e['token']})" if "token" in e else "")
    return [show(e) for e in lab.ledger_events() if e["task"] == task]


# --- the experiments ----------------------------------------------------------------
def e1_worker_dies(backend):
    lab = Lab(backend)
    try:
        task, _ = lab.wait(lambda: holding(lab, "w1", "IN_PROGRESS"), 60, "w1 to hold a task")
        lab.kill("w1")
        finish(lab)
        evs = events(lab, task)
        mech = any(e.startswith("expired") for e in evs) and lab.status()["tasks"][task]["state"] == "SENT"
        return lab, mech, f"w1 killed holding {task}: {' -> '.join(evs)}"
    except Exception:
        lab.close()
        raise


def e2_zombie(backend):
    lab = Lab(backend, work_s=0.8)
    try:
        task, token = freeze_while_holding(lab, "w1", "IN_PROGRESS")
        lab.wait(lambda: lab.status()["tasks"][task]["epoch"] > token, 60, "the task to be reassigned")
        lab.thaw("w1")
        finish(lab)
        fenced = [e for e in lab.journal("w1") if e.get("task") == task and e.get("state") == "fenced"]
        mech = bool(fenced) and lab.status()["counters"]["stale_refused"] >= 1
        return lab, mech, (f"w1 frozen holding {task} (token {token}); after thaw its "
                           f"{fenced[0]['path'] if fenced else '?'} was refused STALE: {' -> '.join(events(lab, task))}")
    except Exception:
        lab.close()
        raise


def e3_duplicate_submit(backend):
    lab = Lab(backend, ledger_flags=["--slow-first-submit-reply", "1.5"])
    try:
        finish(lab)
        dup = [(w, e) for w in lab.ids for e in lab.journal(w) if e.get("duplicate")]
        submits = Counter((e["task"], e["token"]) for e in lab.ledger_events() if e["kind"] == "submitted")
        mech = bool(dup) and all(n == 1 for n in submits.values())
        what = f"{dup[0][0]}'s submit of {dup[0][1]['task']} answered 'duplicate'" if dup else "no duplicate seen"
        return lab, mech, f"first submit reply held 1.5 s > client timeout 1 s; {what}; one 'submitted' per (task, token)"
    except Exception:
        lab.close()
        raise


def e4_late_checker(backend):
    lab = Lab(backend, work_s=1.2)
    try:
        task, token = freeze_while_holding(lab, "c1", "CHECKING")
        lab.wait(lambda: lab.status()["tasks"][task]["epoch"] > token, 60, "the check to be reassigned")
        lab.thaw("c1")
        finish(lab)
        fenced = [e for e in lab.journal("c1") if e.get("task") == task and e.get("state") == "fenced"]
        return lab, bool(fenced), (f"c1 frozen checking {task} (token {token}); after thaw its "
                                   f"{fenced[0]['path'] if fenced else '?'} was refused STALE: {' -> '.join(events(lab, task))}")
    except Exception:
        lab.close()
        raise


def e5_ledger_crash_after_claim(backend):
    lab = Lab(backend, ledger_flags=["--crash-after-claim-once", "crashed.flag"])
    try:
        lab.wait(lambda: lab.procs["ledger"].poll() is not None, 30, "the ledger to crash")
        crashed = json.loads((lab.base / "ledger" / "crashed.flag").read_text())
        lab.start_ledger()                                   # same journal, same port
        finish(lab)
        evs = events(lab, crashed["task"])
        replayed = lab.status()["counters"]["claim_replayed"]
        orphan = any(e.startswith(f"expired") for e in evs[:3])
        return lab, replayed >= 1 and not orphan, (
            f"ledger died after saving {crashed['worker']}'s claim of {crashed['task']} (token {crashed['token']}), "
            f"before replying; restarted; claims replayed: {replayed}; {' -> '.join(evs)}")
    except Exception:
        lab.close()
        raise


def e6_sender_crash_after_send(backend):
    lab = Lab(backend, sender_env={"CRASH_AFTER_DELIVER_ONCE": "crashed.flag"})
    try:
        lab.wait(lambda: (lab.base / "s1" / "crashed.flag").exists() and lab.exited("s1"), 60, "the sender to crash")
        task = (lab.base / "s1" / "crashed.flag").read_text()
        state = lab.status()["tasks"][task]["state"]
        lab.start_sender()
        finish(lab)
        resent = [e for e in lab.journal("s1") if e.get("task") == task and e.get("resend")]
        mech = state == "SENDING" and bool(resent) and resent[0]["duplicate"] and task in lab.mail()["suppressed"]
        return lab, mech, (f"sender died after {task} was delivered, ledger still said {state}; restarted: "
                           f"resent with key {task}, mail service answered 'duplicate'")
    except Exception:
        lab.close()
        raise


def e7_mail_timeout(backend, dedupe=True):
    flags = ["--slow-first-reply", "2.5"] + ([] if dedupe else ["--no-dedupe"])
    lab = Lab(backend, mail_flags=flags)
    try:
        finish(lab)
        m = lab.mail()
        counts = Counter(m["deliveries"])
        if dedupe:
            return lab, bool(m["suppressed"]), (f"first delivery's reply held 2.5 s > sender timeout 1 s; "
                                                f"retried with the same key; suppressed: {m['suppressed']}")
        doubled = [k for k, n in counts.items() if n > 1]
        return lab, bool(doubled), f"same, receiver WITHOUT memory of keys: delivered twice: {doubled}"
    except Exception:
        lab.close()
        raise


def e8_ledger_down(backend):
    lab = Lab(backend, work_s=0.8)
    try:
        lab.wait(lambda: sum(v["state"] == "IN_PROGRESS" for v in lab.status()["tasks"].values()) >= 2, 30,
                 "work in progress")
        held = {t for t, v in lab.status()["tasks"].items() if v["state"] == "IN_PROGRESS"}
        lab.kill_ledger()
        time.sleep(2.0 * SCALE[backend])                      # down for twice the lease
        lab.start_ledger()
        finish(lab)
        expired = {e["task"] for e in lab.ledger_events() if e["kind"] == "expired"}
        return lab, not held & expired, (f"ledger down {2 * SCALE[backend]:.0f} s (lease {SCALE[backend]:.0f} s) while {sorted(held)} were in progress; "
                                         f"after restart none of them expired: {sorted(held & expired) or 'none'}")
    except Exception:
        lab.close()
        raise


EXPERIMENTS = {
    1: ("A worker dies right after claiming", e1_worker_dies),
    2: ("A worker freezes, loses its task, wakes up and submits (the zombie)", e2_zombie),
    3: ("A submit arrives twice (reply lost, client retries)", e3_duplicate_submit),
    4: ("A checker approves late, after the task was re-checked", e4_late_checker),
    5: ("The ledger crashes after saving a claim, before replying", e5_ledger_crash_after_claim),
    6: ("The sender crashes after the email went out, before SENT", e6_sender_crash_after_send),
    7: ("The mail service does not answer in time", e7_mail_timeout),
    8: ("The ledger is down while workers are working", e8_ledger_down),
}


def run(n: int, backend: str):
    title, fn = EXPERIMENTS[n]
    started = time.time()
    lab, mech, story = fn(backend)
    try:
        inv_ok, inv = invariant(lab)
    finally:
        lab.close()
    print(f"\n{n}. {title}  [{time.time() - started:.1f} s]")
    print(f"   predicted: {PREDICTIONS[n]}")
    print(f"   observed:  {story}")
    print(f"   mechanism {'CONFIRMED' if mech else 'NOT SEEN'}; invariant {'HOLDS' if inv_ok else 'BROKEN'}: {inv}")
    if n == 7:
        lab, mech, story = e7_mail_timeout(backend, dedupe=False)
        try:
            inv_ok, inv = invariant(lab)
        finally:
            lab.close()
        print(f"   counterfactual: {story}")
        print(f"   invariant {'HOLDS' if inv_ok else 'BROKEN (as expected without receiver dedupe)'}: {inv}")
    return mech


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="proc", choices=["proc", "container"])
    ap.add_argument("--only", type=int, nargs="*", default=list(EXPERIMENTS))
    a = ap.parse_args()
    for n in a.only:
        run(n, a.backend)
