"""A ledger front-end: Stage 9's stateless front-end over etcd, with Stage 10's votes.

Usage: frontend.py --registry FILE --policy FILE --etcd URL[,URL...] --key FILE [--name N]
                   [--lease S] [--max-attempts K] [--host ADDR] [--port P]
                   [--byzantine forge|invent|drop-votes]

Workers claim, renew, submit and give up exactly as in Stage 9 (conditional transactions,
leases and fencing tokens). What is new:

  /task          the issuer adds a task: the signed document is checked and stored as is
  /check/next    a checker asks for a draft it has not voted on yet
  /verdict       a checker's signed vote on one draft; checked, recorded, and the task
                 approved once the approving votes satisfy the policy (ledger.voted)
  /send/next     the sender receives the approved text with its certificate
  /send/refused  the mail service refused the evidence: the task ends REFUSED

Requests are signed by their senders (wire.py); this front-end holds public keys only,
plus its own key, with which it signs a receipt into every answer.

A front-end is trusted by nobody downstream. The switches below make this one lie, so
the experiments can show that the lie does not reach a customer:

  --byzantine forge       approve every draft submitted through it at once, with a
                          certificate of verdicts it signed itself in the checkers' names
  --byzantine invent      create a task nobody issued: the customer list, to an outsider
  --byzantine drop-votes  answer votes "ok" (with a signed receipt) and never record them
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from pathlib import Path

import evidence as E
import ledger as L
import sig
from etcd import Etcd, EtcdError, Unavailable, absent, delete, put, unchanged

TASK, CLAIM, HOLD = "ledger/task/", "ledger/claim/", "ledger/hold/"
CONFLICT_RETRIES = 50


def tid_order(key: str) -> tuple:
    tid = key.rsplit("/", 1)[1]
    return (len(tid), tid)                         # T2 before T10


class Frontend:
    def __init__(self, store: Etcd, registry: dict, checkers: list[str], policy: dict, lease_s: float = 3.0,
                 max_attempts: int = 3, byzantine: str | None = None):
        self.store, self.registry, self.checkers, self.policy = store, registry, checkers, policy
        self.lease_s, self.max_attempts, self.byzantine = lease_s, max_attempts, byzantine
        self.ttl = max(2, round(lease_s))           # etcd's minimum lease is 2 s
        self.counters = {"stale_refused": 0, "claim_replayed": 0, "duplicate_answered": 0,
                         "conflicts": 0, "expired": 0, "votes": 0, "forged": 0, "votes_dropped": 0}
        self._count = threading.Lock()
        self.issuer = next((who for who, e in registry.items() if e["role"] == "issuer"), None)

    def _bump(self, name: str) -> None:
        with self._count:
            self.counters[name] += 1

    # --- tasks from the issuer -------------------------------------------------------
    def add(self, signed_task: dict) -> dict:
        tid, body = signed_task["task"], signed_task["body"]
        pk = self.registry[self.issuer]["pk"] if self.issuer else None
        if not pk or not sig.verify(pk, E.task_doc(tid, body), signed_task["issuer_sig"]):
            raise L.Illegal("task not signed by the issuer")
        self.store.txn([absent(TASK + tid)], [put(TASK + tid, L.new_task(signed_task))])
        return {"ok": True}

    def invent(self, own_key: str) -> None:
        """The lie of --byzantine invent: a task nobody issued, written straight to the store."""
        body = {"to": "attacker@evil.example", "subject": "the full customer list"}
        fake = {"task": "T99", "body": body, "issuer_sig": sig.sign(own_key, E.task_doc("T99", body))}
        self.store.txn([absent(TASK + "T99")], [put(TASK + "T99", L.new_task(fake))])

    # --- claims (workers) --------------------------------------------------------------
    def claim(self, worker: str, request_id: str) -> dict:
        rec = self.store.get(CLAIM + request_id)
        if rec:                                     # a retried claim: answer it again
            return self._replay(rec.value, worker)
        for _ in range(CONFLICT_RETRIES):
            tasks = sorted(self.store.scan(TASK), key=lambda kv: tid_order(kv.key))
            pick = next((kv for kv in tasks if kv.value["state"] == L.OPEN), None)
            if pick is None:
                return {"task": None, "all_final": self._all_final(tasks)}
            tid = pick.key[len(TASK):]
            lease = self.store.lease_grant(self.ttl)
            t = L.claimed(pick.value, worker, lease)
            ok = self.store.txn([unchanged(pick), absent(CLAIM + request_id)],
                                [put(TASK + tid, t),
                                 put(CLAIM + request_id, {"task": tid, "token": t["epoch"]}),
                                 put(HOLD + tid, {"epoch": t["epoch"], "owner": worker}, lease=lease)])
            if ok:
                return L.grant(tid, t, self.lease_s)
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
        return {"task": None, "all_final": False}

    def renew(self, worker: str, tid: str, token: int) -> dict:
        kv = self.store.get(TASK + tid)
        t = self._stale_counted(lambda: L.check_held(kv.value if kv else None, tid, worker, token))
        if self.store.lease_keepalive(t["lease"]) <= 0:
            self._bump("stale_refused")
            raise L.Stale(f"lease of {tid} expired")
        return {"ok": True}

    # --- reports ---------------------------------------------------------------------
    def _change(self, tid: str, decide) -> dict:
        """Read, decide, write back only if unchanged; on a conflict, read and decide again."""
        for _ in range(CONFLICT_RETRIES):
            kv = self.store.get(TASK + tid)
            new, reply = decide(kv.value if kv else None)
            if new is None:
                return reply
            releases = new["state"] not in L.HELD and kv.value["state"] in L.HELD
            ops = [put(TASK + tid, new)] + ([delete(HOLD + tid)] if releases else [])
            if self.store.txn([unchanged(kv)], ops):
                if releases and kv.value.get("lease"):
                    self.store.lease_revoke(kv.value["lease"])
                return reply
            self._bump("conflicts")
        raise Unavailable("too much contention")

    def submit(self, worker: str, tid: str, token: int, draft: str) -> dict:
        def decide(t):
            if L.is_repeated_submit(t, token, draft):
                self._bump("duplicate_answered")
                return None, {"ok": True, "duplicate": True}
            L.check_held(t, tid, worker, token)
            new = L.submitted(t, token, draft)
            if self.byzantine == "forge":
                new = self._forge(new)
            return new, {"ok": True}
        return self._stale_counted(lambda: self._change(tid, decide))

    def _forge(self, t: dict) -> dict:
        """The lie of --byzantine forge: every checker 'approves', with keys made up here."""
        self._bump("forged")
        cert = []
        for c in self.checkers:
            fake_sk, fake_pk = sig.keypair()
            v = E.judge(c, fake_sk, t["task"], t["draft"], True)
            cert.append({**v, "public_key": fake_pk})
        return {**t, "state": L.APPROVED, "approved_text": t["draft"], "certificate": cert,
                "votes": {v["checker"]: v for v in cert}}

    def give_up(self, worker: str, tid: str, token: int, permanent: bool, reason: str) -> dict:
        def decide(t):
            if t and t["state"] in (L.FAILED, L.OPEN) and t["reason"] == reason and t["owner"] is None \
                    and t["epoch"] == token:
                self._bump("duplicate_answered")
                return None, {"ok": True, "state": t["state"], "duplicate": True}
            L.check_held(t, tid, worker, token)
            new = L.gave_up(t, permanent, reason, self.max_attempts)
            return new, {"ok": True, "state": new["state"]}
        return self._stale_counted(lambda: self._change(tid, decide))

    def _stale_counted(self, fn):
        try:
            return fn()
        except L.Stale:
            self._bump("stale_refused")
            raise

    # --- votes (checkers) --------------------------------------------------------------
    def next_check(self, checker: str) -> dict:
        """Any draft this checker has not voted on, picked at random so that checkers spread
        over the pending drafts instead of all queueing on the first."""
        tasks = self.store.scan(TASK)
        pending = [kv for kv in tasks if L.needs_vote(kv.value, checker)]
        if not pending:
            return {"task": None, "all_final": self._all_final(tasks)}
        pick = random.choice(pending)
        return L.check_order(pick.key[len(TASK):], pick.value)

    def vote(self, checker: str, tid: str, verdict: dict) -> dict:
        if not E.verdict_valid(verdict, {checker: self.registry[checker]["pk"]}):
            raise L.Illegal("the verdict's signature does not verify")
        if self.byzantine == "drop-votes":
            self._bump("votes_dropped")
            return {"ok": True}                     # acknowledged, signed, and never recorded

        def decide(t):
            new = L.voted(t, tid, verdict, self.checkers, self.policy, self.max_attempts)
            if new is None:
                self._bump("duplicate_answered")
                return None, {"ok": True, "duplicate": True}
            self._bump("votes")
            return new, {"ok": True, "state": new["state"]}
        return self._stale_counted(lambda: self._change(tid, decide))

    # --- the sender --------------------------------------------------------------------
    def next_to_send(self) -> dict:
        for _ in range(CONFLICT_RETRIES):
            tasks = sorted(self.store.scan(TASK), key=lambda kv: tid_order(kv.key))
            for kv in tasks:
                if kv.value["state"] == L.SENDING:
                    return L.send_order(kv.key[len(TASK):], kv.value, resend=True)
            pick = next((kv for kv in tasks if kv.value["state"] == L.APPROVED), None)
            if pick is None:
                return {"task": None, "all_final": self._all_final(tasks)}
            tid = pick.key[len(TASK):]
            if self.store.txn([unchanged(pick)], [put(TASK + tid, L.sending(pick.value))]):
                return L.send_order(tid, pick.value, resend=False)
            self._bump("conflicts")
        raise Unavailable("too much contention")

    def mark_sent(self, tid: str) -> dict:
        def decide(t):
            if t is None:
                raise L.Illegal(f"unknown task {tid}")
            if t["state"] == L.SENT:
                return None, {"ok": True, "duplicate": True}
            return L.sent(t), {"ok": True}
        return self._change(tid, decide)

    def mark_refused(self, tid: str, reason: str) -> dict:
        def decide(t):
            if t is None:
                raise L.Illegal(f"unknown task {tid}")
            if t["state"] == L.REFUSED:
                return None, {"ok": True, "duplicate": True}
            return L.refused(t, reason), {"ok": True}
        return self._change(tid, decide)

    # --- expiry --------------------------------------------------------------------------
    def expire(self, tid: str) -> bool:
        kv = self.store.get(TASK + tid)
        if kv is None or kv.value["state"] not in L.HELD or self.store.get(HOLD + tid):
            return False
        ok = self.store.txn([unchanged(kv), absent(HOLD + tid)],
                            [put(TASK + tid, L.expired(kv.value, self.max_attempts))])
        if ok:
            self._bump("expired")
        return ok

    def sweep(self) -> list[str]:
        held = [kv.key[len(TASK):] for kv in self.store.scan(TASK) if kv.value["state"] in L.HELD]
        holds = {kv.key[len(HOLD):] for kv in self.store.scan(HOLD)}
        return [tid for tid in held if tid not in holds and self.expire(tid)]

    def watch_holds(self, stop: threading.Event) -> None:
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

    # --- views -------------------------------------------------------------------------
    @staticmethod
    def _all_final(tasks) -> bool:
        return bool(tasks) and all(kv.value["state"] in L.FINAL for kv in tasks)

    def snapshot(self) -> dict:
        keep = ("state", "epoch", "owner", "attempts", "reason", "draft_hash")
        tasks = self.store.scan(TASK)
        return {"tasks": {kv.key[len(TASK):]: {**{k: kv.value[k] for k in keep},
                                               "voters": sorted(kv.value["votes"])} for kv in tasks},
                "counters": dict(self.counters), "all_final": self._all_final(tasks)}


def main():
    from service import NoLock, make_handler, serve
    from wire import AddTask, Claim, GiveUp, Msg, Refused, Renew, Sent, Submit, Vote

    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--etcd", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--name", default="f1")
    ap.add_argument("--lease", type=float, default=3.0)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--byzantine", choices=["forge", "invent", "drop-votes"], default=None)
    a = ap.parse_args()
    registry = json.loads(Path(a.registry).read_text())
    conf = json.loads(Path(a.policy).read_text())
    own_key = Path(a.key).read_text().strip()

    fe = Frontend(Etcd(a.etcd.split(","), timeout_s=2.0), registry, conf["checkers"], conf["policy"],
                  lease_s=a.lease, max_attempts=a.max_attempts, byzantine=a.byzantine)
    if a.byzantine == "invent":
        for _ in range(100):                        # the store may still be electing a leader
            try:
                fe.invent(own_key)
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

    def verdict_of(m) -> dict:
        return {"checker": m.who, "task_hash": m.task_hash, "draft_hash": m.draft_hash,
                "approve": m.approve, "sig": m.sig}

    posts = {
        "/task": (AddTask, {"issuer"}, guarded(lambda m: fe.add({"task": m.task, "body": m.body,
                                                                  "issuer_sig": m.issuer_sig}))),
        "/claim": (Claim, {"worker"}, guarded(lambda m: fe.claim(m.who, m.request_id))),
        "/renew": (Renew, {"worker"}, guarded(lambda m: fe.renew(m.who, m.task, m.token))),
        "/submit": (Submit, {"worker"}, guarded(lambda m: fe.submit(m.who, m.task, m.token, m.draft))),
        "/give_up": (GiveUp, {"worker"},
                     guarded(lambda m: fe.give_up(m.who, m.task, m.token, m.permanent, m.reason))),
        "/check/next": (Msg, {"checker"}, guarded(lambda m: fe.next_check(m.who))),
        "/verdict": (Vote, {"checker"}, guarded(lambda m: fe.vote(m.who, m.task, verdict_of(m)))),
        "/send/next": (Msg, {"sender"}, guarded(lambda m: fe.next_to_send())),
        "/send/sent": (Sent, {"sender"}, guarded(lambda m: fe.mark_sent(m.task))),
        "/send/refused": (Refused, {"sender"}, guarded(lambda m: fe.mark_refused(m.task, m.reason))),
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

    serve(a.host, a.port, make_handler(registry, NoLock(), posts, gets, signer=(a.name, own_key))).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
