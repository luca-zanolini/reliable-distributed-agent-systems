"""A ledger front-end: stateless; the ledger's rules applied to a replicated store.

Usage: frontend.py --registry FILE --etcd URL[,URL...] [--name N] [--tasks N] [--lease S]
                   [--max-attempts K] [--host ADDR] [--port P]
                   [--crash-after-claim-once FLAGFILE] [--blind-writes]

Workers, checkers and the sender see the same service as in Stage 8 (claim, renew,
submit, give up, verdict, next to send, sent). Behind it there is no state of its own:
every task lives in etcd, replicated on a majority of its members, and every request
becomes

    read the task  ->  apply one rule (ledger.py)  ->  write it back, only if unchanged

as one conditional transaction. Two front-ends (or two threads) racing on the same task
cannot both succeed: the second one's condition fails, it reads again, and decides again.
etcd acknowledges a write only once a majority has it, so nothing is ever answered that a
failover could take back: the reply follows the commit, as in Stage 8 it followed the save.

Keys
  ledger/task/<id>     the task record (state, epoch, owner, lease, draft, ...)
  ledger/claim/<rid>   a claim by request id -> (task, token): retries are recognised
                       wherever they arrive, by whichever front-end
  ledger/hold/<id>     present while the task is held; attached to an etcd lease

Leases. A claim takes an etcd lease (TTL = the lease time) and attaches the hold key to
it, in the same transaction that records the claim. A renewal is a keep-alive on that
lease. If the holder stops renewing, etcd deletes the hold key when the lease runs out;
every front-end watches the hold keys and, on a deletion, reopens the task if it is still
held under the same epoch with no hold (a conditional write: harmless if several try).
A periodic sweep does the same for any deletion a watch missed. Time is measured by
etcd's leader only; when the leader changes, etcd renews every lease. Fencing never looks
at time: a late request with an old token is refused by comparing epochs.

Replies: 200; 409 {"stale": true}; 400 for a malformed request; 503 when the store could
not be reached (no majority): nothing was decided, retry.

Fault injection, for the experiments:
  --crash-after-claim-once F   exit right after a claim commits, before replying, once
  --blind-writes               write without conditions (the counterfactual: lost updates)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import ledger as L
from etcd import Etcd, EtcdError, Unavailable, absent, delete, put, unchanged

TASK, CLAIM, HOLD = "ledger/task/", "ledger/claim/", "ledger/hold/"
CONFLICT_RETRIES = 50


def tid_order(key: str) -> tuple:
    tid = key.rsplit("/", 1)[1]
    return (len(tid), tid)                         # T2 before T10


class Frontend:
    def __init__(self, store: Etcd, lease_s: float = 3.0, max_attempts: int = 3, blind: bool = False,
                 after_commit=None):
        self.store, self.lease_s, self.max_attempts = store, lease_s, max_attempts
        self.ttl = max(2, round(lease_s))           # etcd's minimum lease is 2 s
        self.blind = blind                          # counterfactual: no conditions on writes
        self.after_commit = after_commit            # fault injection: (kind, task) after a commit
        self.counters = {"stale_refused": 0, "claim_replayed": 0, "duplicate_answered": 0,
                         "conflicts": 0, "expired": 0}
        self._count = threading.Lock()

    def _bump(self, name: str) -> None:
        with self._count:
            self.counters[name] += 1

    def _txn(self, compare, success) -> bool:
        return self.store.txn([] if self.blind else compare, success)

    def _committed(self, kind: str, tid: str) -> None:
        if self.after_commit:
            self.after_commit(kind, tid)

    # --- set-up --------------------------------------------------------------------
    def add(self, tid: str, body: dict) -> None:
        self.store.txn([absent(TASK + tid)], [put(TASK + tid, L.new_task(body))])

    # --- claims --------------------------------------------------------------------
    def claim(self, worker: str, request_id: str, role: str) -> dict:
        rec = self.store.get(CLAIM + request_id)
        if rec:                                     # a retried claim: answer it again
            return self._replay(rec.value, worker)
        for _ in range(CONFLICT_RETRIES):
            tasks = sorted(self.store.scan(TASK), key=lambda kv: tid_order(kv.key))
            pick = next((kv for kv in tasks if kv.value["state"] == L.wanted(role)), None)
            if pick is None:
                return {"task": None, "all_final": self._all_final(tasks)}
            tid = pick.key[len(TASK):]
            lease = self.store.lease_grant(self.ttl)
            t = L.claimed(pick.value, worker, lease)
            ok = self._txn([unchanged(pick), absent(CLAIM + request_id)],
                           [put(TASK + tid, t),
                            put(CLAIM + request_id, {"task": tid, "token": t["epoch"]}),
                            put(HOLD + tid, {"epoch": t["epoch"], "owner": worker}, lease=lease)])
            if ok:
                self._committed("claimed", tid)
                return L.grant(tid, t, self.lease_s)
            # Lost a race, or an earlier attempt of this very request went through (a
            # write whose reply was lost is resent to another member and then fails its
            # condition): look for our own claim before giving the lease back.
            rec = self.store.get(CLAIM + request_id)
            if rec:
                return self._replay(rec.value, worker)
            self.store.lease_revoke(lease)
            self._bump("conflicts")
        raise Unavailable("too much contention")

    def _replay(self, rec: dict, worker: str) -> dict:
        kv = self.store.get(TASK + rec["task"])
        t = kv.value if kv else None
        if t and t["state"] in L.HELD and t["epoch"] == rec["token"] and t["owner"] == worker:
            self._bump("claim_replayed")
            return L.grant(rec["task"], t, self.lease_s)
        return {"task": None, "all_final": False}   # that grant already lapsed

    def renew(self, worker: str, tid: str, token: int) -> dict:
        kv = self.store.get(TASK + tid)
        t = self._held(kv, tid, worker, token, L.HELD)
        if self.store.lease_keepalive(t["lease"]) <= 0:
            self._bump("stale_refused")             # the lease ran out: the task is being reopened
            raise L.Stale(f"lease of {tid} expired")
        return {"ok": True}

    def _held(self, kv, tid, worker, token, states) -> dict:
        try:
            return L.check_held(kv.value if kv else None, tid, worker, token, states)
        except L.Stale:
            self._bump("stale_refused")
            raise

    # --- reports from holders ------------------------------------------------------
    def _change(self, tid: str, decide) -> dict:
        """Read, decide, write back only if unchanged; on a conflict, read and decide again.
        decide(t) returns (new task, reply) or raises Stale / Illegal, or returns
        (None, reply) for a request that changes nothing (a recognised repeat)."""
        for _ in range(CONFLICT_RETRIES):
            kv = self.store.get(TASK + tid)
            new, reply = decide(kv.value if kv else None)
            if new is None:
                return reply
            releases = new["state"] not in L.HELD and kv.value["state"] in L.HELD
            ops = [put(TASK + tid, new)] + ([delete(HOLD + tid)] if releases else [])
            if self._txn([unchanged(kv)], ops):
                if releases and kv.value.get("lease"):
                    self.store.lease_revoke(kv.value["lease"])
                self._committed(new["state"], tid)
                return reply
            self._bump("conflicts")
        raise Unavailable("too much contention")

    def submit(self, worker: str, tid: str, token: int, draft: str) -> dict:
        def decide(t):
            if L.is_repeated_submit(t, token, draft):
                self._bump("duplicate_answered")
                return None, {"ok": True, "duplicate": True}
            L.check_held(t, tid, worker, token, {L.IN_PROGRESS})
            return L.submitted(t, token, draft), {"ok": True}
        return self._stale_counted(lambda: self._change(tid, decide))

    def give_up(self, worker: str, tid: str, token: int, permanent: bool, reason: str) -> dict:
        def decide(t):
            if t and t["state"] in (L.FAILED, L.OPEN) and t["reason"] == reason and t["owner"] is None \
                    and t["epoch"] == token:
                self._bump("duplicate_answered")
                return None, {"ok": True, "state": t["state"], "duplicate": True}
            L.check_held(t, tid, worker, token, {L.IN_PROGRESS})
            new = L.gave_up(t, permanent, reason, self.max_attempts)
            return new, {"ok": True, "state": new["state"]}
        return self._stale_counted(lambda: self._change(tid, decide))

    def verdict(self, worker: str, tid: str, token: int, draft_hash: str, approve: bool,
                reason: str) -> dict:
        def decide(t):
            if L.is_repeated_verdict(t, token, approve):
                self._bump("duplicate_answered")
                return None, {"ok": True, "duplicate": True}
            L.check_held(t, tid, worker, token, {L.CHECKING})
            if draft_hash != t["draft_hash"]:
                raise L.Illegal(f"verdict names draft {draft_hash}, but {tid} is checking {t['draft_hash']}")
            new = L.judged(t, token, approve, reason, self.max_attempts)
            return new, {"ok": True, "state": new["state"]}
        return self._stale_counted(lambda: self._change(tid, decide))

    def _stale_counted(self, fn):
        try:
            return fn()
        except L.Stale:
            self._bump("stale_refused")
            raise

    # --- the sender ----------------------------------------------------------------
    def next_to_send(self) -> dict:
        """An unfinished send first (its outcome is unknown: resend with the same key),
        otherwise the next approved task, recording the intent before handing it out."""
        for _ in range(CONFLICT_RETRIES):
            tasks = sorted(self.store.scan(TASK), key=lambda kv: tid_order(kv.key))
            for kv in tasks:
                if kv.value["state"] == L.SENDING:
                    return L.send_order(kv.key[len(TASK):], kv.value, resend=True)
            pick = next((kv for kv in tasks if kv.value["state"] == L.APPROVED), None)
            if pick is None:
                return {"task": None, "all_final": self._all_final(tasks)}
            tid = pick.key[len(TASK):]
            t = L.sending(pick.value)
            if self._txn([unchanged(pick)], [put(TASK + tid, t)]):
                self._committed("sending", tid)
                return L.send_order(tid, t, resend=False)
            self._bump("conflicts")
        raise Unavailable("too much contention")

    def mark_sent(self, tid: str) -> dict:
        def decide(t):
            if t is None:
                raise L.Illegal(f"unknown task {tid}")
            if t["state"] == L.SENT:
                self._bump("duplicate_answered")
                return None, {"ok": True, "duplicate": True}
            return L.sent(t), {"ok": True}
        return self._change(tid, decide)

    # --- expiry --------------------------------------------------------------------
    def expire(self, tid: str) -> bool:
        """Reopen `tid` if it is held but its hold is gone (its lease ran out)."""
        kv = self.store.get(TASK + tid)
        if kv is None or kv.value["state"] not in L.HELD or self.store.get(HOLD + tid):
            return False
        ok = self._txn([unchanged(kv), absent(HOLD + tid)], [put(TASK + tid, L.expired(kv.value, self.max_attempts))])
        if ok:
            self._bump("expired")
        return ok

    def sweep(self) -> list[str]:
        held = [kv.key[len(TASK):] for kv in self.store.scan(TASK) if kv.value["state"] in L.HELD]
        holds = {kv.key[len(HOLD):] for kv in self.store.scan(HOLD)}
        return [tid for tid in held if tid not in holds and self.expire(tid)]

    def watch_holds(self, stop: threading.Event) -> None:
        """Reopen a task as soon as its hold key is deleted. The stream breaks when the
        member serving it fails; then watch again (the sweep covers the gap)."""
        while not stop.is_set():
            try:
                for kind, key, _rev in self.store.watch(HOLD):
                    if kind == "DELETE":
                        try:
                            self.expire(key[len(HOLD):])
                        except EtcdError:
                            pass
                    if stop.is_set():
                        return
            except (EtcdError, OSError):
                pass
            stop.wait(0.2)

    # --- views ---------------------------------------------------------------------
    @staticmethod
    def _all_final(tasks) -> bool:
        return bool(tasks) and all(kv.value["state"] in L.FINAL for kv in tasks)

    def snapshot(self) -> dict:
        keep = ("state", "epoch", "owner", "attempts", "reason", "draft_hash")
        tasks = self.store.scan(TASK)
        return {"tasks": {kv.key[len(TASK):]: {k: kv.value[k] for k in keep} for kv in tasks},
                "counters": dict(self.counters), "all_final": self._all_final(tasks)}


def main():
    from service import make_handler, serve
    from wire import Claim, GiveUp, Msg, Renew, Sent, Submit, Verdict

    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--etcd", required=True)
    ap.add_argument("--name", default="f1")
    ap.add_argument("--tasks", type=int, default=8)
    ap.add_argument("--lease", type=float, default=3.0)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--crash-after-claim-once", default=None)
    ap.add_argument("--blind-writes", action="store_true")
    a = ap.parse_args()
    registry = json.loads(Path(a.registry).read_text())

    def after_commit(kind, tid):
        flag = a.crash_after_claim_once
        if flag and kind == "claimed" and not os.path.exists(flag):
            Path(flag).write_text(json.dumps({"front_end": a.name, "task": tid}))
            os._exit(70)                            # committed, never answered

    fe = Frontend(Etcd(a.etcd.split(","), timeout_s=2.0), lease_s=a.lease, max_attempts=a.max_attempts,
                  blind=a.blind_writes, after_commit=after_commit)
    for _ in range(100):                            # the store may still be electing its first leader
        try:
            for i in range(1, a.tasks + 1):
                to = "not-an-address" if i == a.tasks else f"customer{i}@example.com"
                fe.add(f"T{i}", {"to": to, "subject": f"Report {i}"})
            break
        except Unavailable:
            time.sleep(0.2)

    def guarded(fn):
        def run(msg):
            try:
                return 200, fn(msg)
            except L.Stale as e:
                return 409, {"stale": True, "error": str(e)}
            except L.Illegal as e:
                return 400, {"error": str(e)}
            except Unavailable as e:
                return 503, {"error": f"store unavailable: {e}"}
        return run

    role_of = {who: entry["role"] for who, entry in registry.items()}
    posts = {
        "/claim": (Claim, {"worker", "checker"}, guarded(lambda m: fe.claim(m.who, m.request_id, role_of[m.who]))),
        "/renew": (Renew, {"worker", "checker"}, guarded(lambda m: fe.renew(m.who, m.task, m.token))),
        "/submit": (Submit, {"worker"}, guarded(lambda m: fe.submit(m.who, m.task, m.token, m.draft))),
        "/give_up": (GiveUp, {"worker"},
                     guarded(lambda m: fe.give_up(m.who, m.task, m.token, m.permanent, m.reason))),
        "/verdict": (Verdict, {"checker"},
                     guarded(lambda m: fe.verdict(m.who, m.task, m.token, m.draft_hash, m.approve, m.reason))),
        "/send/next": (Msg, {"sender"}, guarded(lambda m: fe.next_to_send())),
        "/send/sent": (Sent, {"sender"}, guarded(lambda m: fe.mark_sent(m.task))),
    }

    def status():
        try:
            return {"front_end": a.name, **fe.snapshot()}
        except Unavailable as e:
            return {"front_end": a.name, "error": str(e), "counters": dict(fe.counters)}

    gets = {"/health": lambda: {"ok": True, "front_end": a.name}, "/status": status}

    stop = threading.Event()
    threading.Thread(target=fe.watch_holds, args=(stop,), daemon=True).start()

    def sweeper():
        while not stop.wait(a.lease / 3):
            try:
                fe.sweep()
            except EtcdError:
                pass
    threading.Thread(target=sweeper, daemon=True).start()

    # No lock around requests: the store serialises conflicting writes, and a front-end
    # may serve requests concurrently exactly as two front-ends do.
    serve(a.host, a.port, make_handler(registry, _NoLock(), posts, gets)).serve_forever()


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    sys.exit(main())
