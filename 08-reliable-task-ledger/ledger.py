"""The ledger: the authoritative, durable record of every task.

A task moves through these states, and only along these edges:

    OPEN -claim-> IN_PROGRESS -submit-> UNDER_CHECK -claim-> CHECKING -approve-> APPROVED
      ^                |                     ^                  |                   |
      |  lease expires |                     |   lease expires  |              sending (intent)
      +----------------+                     +------------------+                   v
      +--------------------------- reject ---------------------+             SENDING -sent-> SENT
    FAILED: a permanent error, or the attempt budget used up. SENT and FAILED are final.

Ownership. A claim gives the claimer a lease (a time limit, renewed while it works)
and a token: the task's epoch, a per-task counter incremented on every claim. Every
later request about the task (renew, submit, give up, verdict) carries the token and
is accepted only while the task is still held under that epoch by that claimer.
Anything else is STALE: the claimer lost the task and must stop. Leases are measured
by the ledger's own monotonic clock only; fencing does not depend on time at all.

Durability. Every change is first appended to a journal and forced to disk, then
applied, then answered: the outside world never sees a state the disk does not know.
At start-up the state is rebuilt by replaying the journal. Leases are not journaled:
after a restart every held task gets a fresh full lease, so a live owner can renew.

Repeats. A retried claim carries the same request id and gets the same task and token
back; a retried submit or verdict (same token, same draft) is answered "already done".

The irreversible effect (sending the approved text) is recorded as an intent, SENDING,
before it is attempted; the sender always sends the pinned approved text with the task
id as idempotency key, so a send repeated after a crash is suppressed by the receiver.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import zlib
from pathlib import Path

OPEN, IN_PROGRESS, UNDER_CHECK, CHECKING = "OPEN", "IN_PROGRESS", "UNDER_CHECK", "CHECKING"
APPROVED, SENDING, SENT, FAILED = "APPROVED", "SENDING", "SENT", "FAILED"
FINAL = {SENT, FAILED}
HELD = {IN_PROGRESS, CHECKING}                    # states in which a claimer holds a lease

# event kind -> states it may be applied in. Anything else is a bug, not a request error.
LEGAL = {
    "added": {None},
    "claimed": {OPEN, UNDER_CHECK},
    "submitted": {IN_PROGRESS},
    "gave_up": {IN_PROGRESS},
    "expired": {IN_PROGRESS, CHECKING},
    "judged": {CHECKING},
    "sending": {APPROVED},
    "sent": {SENDING},
}


class Stale(Exception):
    """The requester no longer holds the task (or never did)."""


class Illegal(Exception):
    """The request is malformed for this task: unknown task, wrong draft, wrong role."""


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Journal:
    """Append-only, one JSON record per line, each with a CRC32 of its event.

    A crash can leave at most the last line torn (it was never confirmed to anyone,
    because replies are sent only after the append returns); replay drops it and
    truncates the file there. A bad line anywhere else is not a crash artifact and is
    reported as corruption.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.touch()

    def append(self, event: dict) -> None:
        body = json.dumps(event, sort_keys=True)
        line = json.dumps({"crc": zlib.crc32(body.encode()), "e": body}) + "\n"
        with open(self.path, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def replay(self) -> list[dict]:
        raw = self.path.read_bytes()
        events, offset = [], 0
        lines = raw.split(b"\n")
        for i, line in enumerate(lines):
            if not line:
                offset += 1
                continue
            try:
                rec = json.loads(line)
                if zlib.crc32(rec["e"].encode()) != rec["crc"]:
                    raise ValueError("checksum mismatch")
                events.append(json.loads(rec["e"]))
            except (ValueError, KeyError, TypeError):
                if any(lines[i + 1:]):
                    raise RuntimeError(f"journal corrupt at byte {offset}") from None
                with open(self.path, "r+b") as f:       # torn tail: drop it
                    f.truncate(offset)
                break
            offset += len(line) + 1
        return events


class Ledger:
    def __init__(self, journal_path: str | Path, lease_s: float = 2.0, max_attempts: int = 3,
                 clock=time.monotonic, after_save=None):
        self.journal = Journal(journal_path)
        self.lease_s, self.max_attempts, self.clock = lease_s, max_attempts, clock
        self.after_save = after_save              # fault injection: called after a save, before replying
        self.tasks: dict[str, dict] = {}
        self.claims: dict[str, tuple[str, int]] = {}   # request id -> (task, token)
        self.deadline: dict[str, float] = {}           # volatile: never journaled
        self.counters = {"stale_refused": 0, "claim_replayed": 0, "duplicate_answered": 0}
        for event in self.journal.replay():
            self._apply(event)
        now = self.clock()
        for tid, t in self.tasks.items():
            if t["state"] in HELD:
                self.deadline[tid] = now + self.lease_s   # fresh full lease after a restart

    # --- the only way state changes: save, then apply ------------------------
    def _record(self, **event) -> None:
        self.journal.append(event)
        if self.after_save:
            self.after_save(event)
        self._apply(event)

    def _apply(self, e: dict) -> None:
        kind, tid = e["kind"], e["task"]
        t = self.tasks.get(tid)
        state = t["state"] if t else None
        if state not in LEGAL[kind]:
            raise RuntimeError(f"illegal transition: {kind} on {tid} in state {state}")
        if kind == "added":
            self.tasks[tid] = {"state": OPEN, "epoch": 0, "owner": None, "attempts": 0,
                               "body": e["body"], "draft": None, "draft_hash": None,
                               "submitted_token": None, "judged": None, "approved_text": None,
                               "reason": None}
        elif kind == "claimed":
            t["epoch"], t["owner"] = e["token"], e["worker"]
            if state == OPEN:
                t["state"], t["attempts"] = IN_PROGRESS, t["attempts"] + 1
            else:
                t["state"] = CHECKING
            self.claims[e["request_id"]] = (tid, e["token"])
        elif kind == "submitted":
            t["state"], t["owner"] = UNDER_CHECK, None
            t["draft"], t["draft_hash"], t["submitted_token"] = e["draft"], fingerprint(e["draft"]), e["token"]
        elif kind == "expired":
            t["owner"] = None
            t["state"] = UNDER_CHECK if state == CHECKING else self._reopen(t, "lease expired")
        elif kind == "gave_up":
            t["owner"], t["reason"] = None, e["reason"]
            t["state"] = FAILED if e["permanent"] else self._reopen(t, e["reason"])
        elif kind == "judged":
            t["owner"], t["judged"] = None, (e["token"], e["approve"])
            if e["approve"]:
                t["state"], t["approved_text"] = APPROVED, t["draft"]
            else:
                t["state"] = self._reopen(t, e["reason"])
        elif kind == "sending":
            t["state"] = SENDING
        elif kind == "sent":
            t["state"] = SENT
        if kind in ("submitted", "expired", "gave_up", "judged"):
            self.deadline.pop(tid, None)

    def _reopen(self, t: dict, reason: str) -> str:
        t["reason"] = reason
        return FAILED if t["attempts"] >= self.max_attempts else OPEN

    def _held(self, tid: str, worker: str, token: int, states: set[str]) -> dict:
        t = self.tasks.get(tid)
        if t is None:
            raise Illegal(f"unknown task {tid}")
        if t["state"] not in states or t["epoch"] != token or t["owner"] != worker:
            self.counters["stale_refused"] += 1
            raise Stale(f"{worker} with token {token} does not hold {tid} "
                        f"(state {t['state']}, epoch {t['epoch']})")
        return t

    # --- requests ------------------------------------------------------------
    def add(self, tid: str, body: dict) -> None:
        if tid not in self.tasks:
            self._record(kind="added", task=tid, body=body)

    def claim(self, worker: str, request_id: str, role: str) -> dict:
        if request_id in self.claims:                       # a retried claim
            tid, token = self.claims[request_id]
            t = self.tasks[tid]
            if t["state"] in HELD and t["epoch"] == token and t["owner"] == worker:
                self.counters["claim_replayed"] += 1
                return self._grant(tid, token)
            return {"task": None, "all_final": self.all_final()}  # that grant already lapsed
        wanted = OPEN if role == "worker" else UNDER_CHECK
        for tid, t in self.tasks.items():
            if t["state"] == wanted:
                token = t["epoch"] + 1                      # never reused: epochs only grow
                self._record(kind="claimed", task=tid, worker=worker, token=token,
                             request_id=request_id)
                self.deadline[tid] = self.clock() + self.lease_s
                return self._grant(tid, token)
        return {"task": None, "all_final": self.all_final()}

    def _grant(self, tid: str, token: int) -> dict:
        t = self.tasks[tid]
        return {"task": tid, "token": token, "body": t["body"], "attempt": t["attempts"],
                "draft": t["draft"] if t["state"] == CHECKING else None, "lease_s": self.lease_s}

    def renew(self, worker: str, tid: str, token: int) -> dict:
        self._held(tid, worker, token, HELD)
        self.deadline[tid] = self.clock() + self.lease_s
        return {"ok": True}

    def submit(self, worker: str, tid: str, token: int, draft: str) -> dict:
        t = self.tasks.get(tid)
        if t and t["submitted_token"] == token and t["draft_hash"] == fingerprint(draft):
            self.counters["duplicate_answered"] += 1
            return {"ok": True, "duplicate": True}         # this exact submit was already accepted
        self._held(tid, worker, token, {IN_PROGRESS})
        self._record(kind="submitted", task=tid, worker=worker, token=token, draft=draft)
        return {"ok": True}

    def give_up(self, worker: str, tid: str, token: int, permanent: bool, reason: str) -> dict:
        self._held(tid, worker, token, {IN_PROGRESS})
        self._record(kind="gave_up", task=tid, worker=worker, token=token,
                     permanent=permanent, reason=reason)
        return {"ok": True, "state": self.tasks[tid]["state"]}

    def verdict(self, worker: str, tid: str, token: int, draft_hash: str, approve: bool,
                reason: str) -> dict:
        t = self.tasks.get(tid)
        if t and t["judged"] == (token, approve):
            self.counters["duplicate_answered"] += 1
            return {"ok": True, "duplicate": True}
        t = self._held(tid, worker, token, {CHECKING})
        if draft_hash != t["draft_hash"]:
            raise Illegal(f"verdict names draft {draft_hash}, but {tid} is checking {t['draft_hash']}")
        self._record(kind="judged", task=tid, worker=worker, token=token, approve=approve,
                     reason=reason)
        return {"ok": True, "state": self.tasks[tid]["state"]}

    def next_to_send(self) -> dict:
        """An unfinished send first (its outcome is unknown: resend with the same key),
        otherwise the next approved task, recording the intent before handing it out."""
        for tid, t in self.tasks.items():
            if t["state"] == SENDING:
                return self._send_order(tid, resend=True)
        for tid, t in self.tasks.items():
            if t["state"] == APPROVED:
                self._record(kind="sending", task=tid)
                return self._send_order(tid, resend=False)
        return {"task": None, "all_final": self.all_final()}

    def _send_order(self, tid: str, resend: bool) -> dict:
        t = self.tasks[tid]
        return {"task": tid, "key": tid, "to": t["body"]["to"], "subject": t["body"]["subject"],
                "text": t["approved_text"], "resend": resend}

    def mark_sent(self, tid: str) -> dict:
        t = self.tasks.get(tid)
        if t is None:
            raise Illegal(f"unknown task {tid}")
        if t["state"] == SENT:
            self.counters["duplicate_answered"] += 1
            return {"ok": True, "duplicate": True}
        if t["state"] != SENDING:
            raise Illegal(f"{tid} is {t['state']}, not SENDING")
        self._record(kind="sent", task=tid)
        return {"ok": True}

    def expire(self) -> list[str]:
        now = self.clock()
        lapsed = [tid for tid, d in self.deadline.items() if d <= now]
        for tid in lapsed:
            self._record(kind="expired", task=tid, token=self.tasks[tid]["epoch"])
        return lapsed

    # --- views ---------------------------------------------------------------
    def all_final(self) -> bool:
        return bool(self.tasks) and all(t["state"] in FINAL for t in self.tasks.values())

    def snapshot(self) -> dict:
        keep = ("state", "epoch", "owner", "attempts", "reason", "draft_hash")
        return {"tasks": {tid: {k: t[k] for k in keep} for tid, t in self.tasks.items()},
                "counters": dict(self.counters), "all_final": self.all_final()}
