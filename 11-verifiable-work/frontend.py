"""A ledger front-end: Stage 10's stateless front-end over etcd, with Stage 11's evidence.

Usage: frontend.py --registry FILE --policy FILE --etcd URL[,URL...] --key FILE --repo URL
                   [--name N] [--lease S] [--max-attempts K] [--host ADDR] [--port P]
                   [--byzantine skip-review]

Workers claim, renew and submit as before (conditional transactions, leases, fencing
tokens); what they submit is a version in the repository and the base it was built on.
To route the work, the front-end asks the repository which paths the change touches, and
so which review it needs (ledger.submitted).

  /task          the issuer adds a signed task
  /check/next    a verifier asks for a version it has not attested yet
  /attest        a verifier's signed attestation
  /review/next   a reviewer asks for a change it has not reviewed yet
  /review        a reviewer's signed review
  /commit/next   the committer receives an approved version with its certificate
  /commit/done, /commit/refused

  --byzantine skip-review   ignore the review the issuer's rules require (route nothing to
                            reviewers): a ledger that lets a change through on tests alone
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import evidence as E
import ledger as L
import sig
from etcd import Etcd, EtcdError, Unavailable, absent, delete, put, unchanged
from wire import get_json

TASK, CLAIM, HOLD = "ledger/task/", "ledger/claim/", "ledger/hold/"
CONFLICT_RETRIES = 50


def tid_order(key: str) -> tuple:
    tid = key.rsplit("/", 1)[1]
    return (len(tid), tid)


class Frontend:
    def __init__(self, store, registry: dict, verifiers: list[str], reviewer_kinds: dict, policy: dict,
                 changed_paths, lease_s: float = 3.0, max_attempts: int = 3, byzantine: str | None = None):
        """changed_paths(base, result) -> the paths that differ, as the repository says."""
        self.store, self.registry, self.verifiers, self.reviewer_kinds = store, registry, verifiers, reviewer_kinds
        self.policy, self.changed_paths = policy, changed_paths
        self.lease_s, self.max_attempts, self.byzantine = lease_s, max_attempts, byzantine
        self.ttl = max(2, round(lease_s))
        self.counters = {"stale_refused": 0, "duplicate_answered": 0, "conflicts": 0, "expired": 0,
                         "attestations": 0, "reviews": 0, "reviews_skipped": 0}
        self._count = threading.Lock()
        self.issuer = next((w for w, e in registry.items() if e["role"] == "issuer"), None)

    def _bump(self, name: str) -> None:
        with self._count:
            self.counters[name] += 1

    def _args(self):
        return self.verifiers, self.reviewer_kinds, self.policy, self.max_attempts

    # --- tasks ------------------------------------------------------------------------------
    def add(self, signed_task: dict) -> dict:
        tid, body = signed_task["task"], signed_task["body"]
        pk = self.registry[self.issuer]["pk"] if self.issuer else None
        if not pk or not sig.verify(pk, E.task_doc(tid, body), signed_task["issuer_sig"]):
            raise L.Illegal("task not signed by the issuer")
        self.store.txn([absent(TASK + tid)], [put(TASK + tid, L.new_task(signed_task))])
        return {"ok": True}

    # --- workers (Stage 9's claims) -------------------------------------------------------------
    def claim(self, worker: str, request_id: str) -> dict:
        rec = self.store.get(CLAIM + request_id)
        if rec:
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
                                [put(TASK + tid, t), put(CLAIM + request_id, {"task": tid, "token": t["epoch"]}),
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
            return L.grant(rec["task"], t, self.lease_s)
        return {"task": None, "all_final": False}

    def renew(self, worker: str, tid: str, token: int) -> dict:
        kv = self.store.get(TASK + tid)
        t = self._stale_counted(lambda: L.check_held(kv.value if kv else None, tid, worker, token))
        if self.store.lease_keepalive(t["lease"]) <= 0:
            self._bump("stale_refused")
            raise L.Stale(f"lease of {tid} expired")
        return {"ok": True}

    def _change(self, tid: str, decide) -> dict:
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

    def _stale_counted(self, fn):
        try:
            return fn()
        except L.Stale:
            self._bump("stale_refused")
            raise

    def submit(self, worker: str, tid: str, token: int, base: str, result: str) -> dict:
        changed = self.changed_paths(base, result)
        if changed is None:
            raise L.Illegal("the repository does not know that version")

        def decide(t):
            if t and t["submitted_token"] == token and (t["base"], t["result"]) == (base, result):
                self._bump("duplicate_answered")
                return None, {"ok": True, "duplicate": True}
            L.check_held(t, tid, worker, token)
            new = L.submitted(t, token, base, result, changed)
            if self.byzantine == "skip-review" and new["review"] != "none":
                self._bump("reviews_skipped")
                new = {**new, "review": "none"}
            return new, {"ok": True, "review": new["review"]}
        return self._stale_counted(lambda: self._change(tid, decide))

    # --- evidence ------------------------------------------------------------------------------
    def _pick(self, wanted) -> dict:
        tasks = self.store.scan(TASK)
        pending = [kv for kv in tasks if wanted(kv.value)]
        if not pending:
            return {"task": None, "all_final": self._all_final(tasks)}
        pick = random.choice(pending)
        return L.check_order(pick.key[len(TASK):], pick.value)

    def next_check(self, verifier: str) -> dict:
        return self._pick(lambda t: L.needs_attestation(t, verifier))

    def next_review(self, reviewer: str) -> dict:
        return self._pick(lambda t: L.needs_review(t, reviewer))

    def attest(self, verifier: str, tid: str, a: dict) -> dict:
        if a.get("verifier") != verifier or not E.attestation_valid(a, {verifier: self.registry[verifier]["pk"]}):
            raise L.Illegal("the attestation is not signed by its sender")

        def decide(t):
            new = L.attested(t, tid, a, *self._args())
            if new is None:
                return None, {"ok": True, "duplicate": True}
            self._bump("attestations")
            return new, {"ok": True, "state": new["state"]}
        return self._stale_counted(lambda: self._change(tid, decide))

    def review(self, reviewer: str, tid: str, r: dict) -> dict:
        if r.get("reviewer") != reviewer or not E.review_valid(r, {reviewer: self.registry[reviewer]["pk"]}):
            raise L.Illegal("the review is not signed by its sender")

        def decide(t):
            new = L.reviewed(t, tid, r, *self._args())
            if new is None:
                return None, {"ok": True, "duplicate": True}
            self._bump("reviews")
            return new, {"ok": True, "state": new["state"]}
        return self._stale_counted(lambda: self._change(tid, decide))

    # --- the committer -----------------------------------------------------------------------------
    def next_commit(self) -> dict:
        for _ in range(CONFLICT_RETRIES):
            tasks = sorted(self.store.scan(TASK), key=lambda kv: tid_order(kv.key))
            for kv in tasks:
                if kv.value["state"] == L.COMMITTING:
                    return L.commit_order(kv.key[len(TASK):], kv.value)
            pick = next((kv for kv in tasks if kv.value["state"] == L.APPROVED), None)
            if pick is None:
                return {"task": None, "all_final": self._all_final(tasks)}
            tid = pick.key[len(TASK):]
            if self.store.txn([unchanged(pick)], [put(TASK + tid, L.committing(pick.value))]):
                return L.commit_order(tid, pick.value)
            self._bump("conflicts")
        raise Unavailable("too much contention")

    def mark_committed(self, tid: str) -> dict:
        def decide(t):
            if t is None:
                raise L.Illegal(f"unknown task {tid}")
            if t["state"] == L.COMMITTED:
                return None, {"ok": True, "duplicate": True}
            return L.committed(t), {"ok": True}
        return self._change(tid, decide)

    def mark_refused(self, tid: str, reason: str) -> dict:
        def decide(t):
            if t is None:
                raise L.Illegal(f"unknown task {tid}")
            if t["state"] != L.COMMITTING:
                return None, {"ok": True, "duplicate": True}
            new = L.commit_refused(t, reason, self.max_attempts)
            return new, {"ok": True, "state": new["state"]}
        return self._change(tid, decide)

    # --- expiry (Stage 9's) ------------------------------------------------------------------------
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

    # --- views -------------------------------------------------------------------------------------
    @staticmethod
    def _all_final(tasks) -> bool:
        return bool(tasks) and all(kv.value["state"] in L.FINAL for kv in tasks)

    def snapshot(self) -> dict:
        tasks = self.store.scan(TASK)
        return {"tasks": {kv.key[len(TASK):]: {
            "state": kv.value["state"], "epoch": kv.value["epoch"], "attempts": kv.value["attempts"],
            "reason": kv.value["reason"], "review": kv.value["review"], "result": kv.value["result"],
            "attestations": {v: a["outcome"] for v, a in kv.value["attestations"].items()},
            "reviews": {r: v["approve"] for r, v in kv.value["reviews"].items()}} for kv in tasks},
            "counters": dict(self.counters), "all_final": self._all_final(tasks)}


def main():
    from service import NoLock, make_handler, serve
    from wire import AddTask, Attest, Claim, Done, Msg, Refused, Renew, Review, Submit

    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--etcd", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--name", default="f1")
    ap.add_argument("--lease", type=float, default=3.0)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--byzantine", choices=["skip-review"], default=None)
    a = ap.parse_args()
    registry = json.loads(Path(a.registry).read_text())
    conf = json.loads(Path(a.policy).read_text())
    own_key = Path(a.key).read_text().strip()

    def changed_paths(base, result):
        q = urllib.parse.urlencode({"base": base, "result": result})
        out = get_json(f"{a.repo}/diff?{q}", timeout_s=3.0)
        return out["changed"] if out and "changed" in out else None

    fe = Frontend(Etcd(a.etcd.split(","), timeout_s=2.0), registry, conf["verifiers"], conf["reviewers"],
                  conf["policy"], changed_paths, lease_s=a.lease, max_attempts=a.max_attempts, byzantine=a.byzantine)

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

    posts = {
        "/task": (AddTask, {"issuer"}, guarded(lambda m: fe.add({"task": m.task, "body": m.body, "issuer_sig": m.issuer_sig}))),
        "/claim": (Claim, {"worker"}, guarded(lambda m: fe.claim(m.who, m.request_id))),
        "/renew": (Renew, {"worker"}, guarded(lambda m: fe.renew(m.who, m.task, m.token))),
        "/submit": (Submit, {"worker"}, guarded(lambda m: fe.submit(m.who, m.task, m.token, m.base, m.result))),
        "/check/next": (Msg, {"verifier"}, guarded(lambda m: fe.next_check(m.who))),
        "/attest": (Attest, {"verifier"}, guarded(lambda m: fe.attest(m.who, m.task, m.attestation))),
        "/review/next": (Msg, {"reviewer"}, guarded(lambda m: fe.next_review(m.who))),
        "/review": (Review, {"reviewer"}, guarded(lambda m: fe.review(m.who, m.task, m.review))),
        "/commit/next": (Msg, {"committer"}, guarded(lambda m: fe.next_commit())),
        "/commit/done": (Done, {"committer"}, guarded(lambda m: fe.mark_committed(m.task))),
        "/commit/refused": (Refused, {"committer"}, guarded(lambda m: fe.mark_refused(m.task, m.reason))),
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
