"""The ledger as a service on the lab host.

Usage: ledger_service.py --registry FILE --journal FILE [--tasks N] [--lease S]
                         [--max-attempts K] [--host ADDR] [--port P]
                         [--crash-after-claim-once FLAGFILE] [--slow-first-submit-reply S]

The journal path is what makes the ledger durable: restart it with the same journal
and it rebuilds every task. Tasks T1..TN are added on first start; the last one has
an invalid address (a permanent failure), so the FAILED path is exercised too.

Who may ask for what (least privilege, by role in the registry):
  worker   claim (an OPEN task), renew, submit, give up
  checker  claim (a task UNDER_CHECK), renew, verdict
  sender   next to send, mark sent

Replies: 200 with a result; 409 {"stale": true} when the requester no longer holds
the task (stop and discard the work); 400 for a request that names the wrong task or
draft. A background thread expires lapsed leases.

Fault injection, for the experiments:
  --crash-after-claim-once F   exit immediately after saving a claim, before replying,
                               the first time only (F records that it happened)
  --slow-first-submit-reply S  apply the first submit, then hold its reply S seconds,
                               so the worker times out and retries it
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from ledger import Illegal, Ledger, Stale
from service import make_handler, serve
from wire import Claim, GiveUp, Msg, Renew, Sent, Submit, Verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--tasks", type=int, default=8)
    ap.add_argument("--lease", type=float, default=2.0)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--crash-after-claim-once", default=None)
    ap.add_argument("--slow-first-submit-reply", type=float, default=0.0)
    a = ap.parse_args()
    registry = json.loads(Path(a.registry).read_text())

    def after_save(event):
        flag = a.crash_after_claim_once
        if flag and event["kind"] == "claimed" and not os.path.exists(flag):
            Path(flag).write_text(json.dumps(event))
            os._exit(70)                           # saved, never answered

    ledger = Ledger(a.journal, lease_s=a.lease, max_attempts=a.max_attempts, after_save=after_save)
    for i in range(1, a.tasks + 1):
        to = "not-an-address" if i == a.tasks else f"customer{i}@example.com"
        ledger.add(f"T{i}", {"to": to, "subject": f"Report {i}"})
    lock = threading.Lock()

    def guarded(fn):
        def run(msg):
            try:
                return 200, fn(msg)
            except Stale as e:
                return 409, {"stale": True, "error": str(e)}
            except Illegal as e:
                return 400, {"error": str(e)}
        return run

    role_of = {who: entry["role"] for who, entry in registry.items()}
    posts = {
        "/claim": (Claim, {"worker", "checker"},
                   guarded(lambda m: ledger.claim(m.who, m.request_id, role_of[m.who]))),
        "/renew": (Renew, {"worker", "checker"}, guarded(lambda m: ledger.renew(m.who, m.task, m.token))),
        "/submit": (Submit, {"worker"}, guarded(lambda m: ledger.submit(m.who, m.task, m.token, m.draft))),
        "/give_up": (GiveUp, {"worker"},
                     guarded(lambda m: ledger.give_up(m.who, m.task, m.token, m.permanent, m.reason))),
        "/verdict": (Verdict, {"checker"},
                     guarded(lambda m: ledger.verdict(m.who, m.task, m.token, m.draft_hash, m.approve, m.reason))),
        "/send/next": (Msg, {"sender"}, guarded(lambda m: ledger.next_to_send())),
        "/send/sent": (Sent, {"sender"}, guarded(lambda m: ledger.mark_sent(m.task))),
    }
    gets = {"/health": lambda: {"ok": True}, "/status": ledger.snapshot}

    slowed = []

    def slow_reply(path, out):
        if path == "/submit" and a.slow_first_submit_reply and not slowed and not out.get("duplicate"):
            slowed.append(True)
            return a.slow_first_submit_reply
        return 0

    def expirer():
        while True:
            time.sleep(a.lease / 5)
            with lock:
                ledger.expire()
    threading.Thread(target=expirer, daemon=True).start()

    serve(a.host, a.port, make_handler(registry, lock, posts, gets, slow_reply)).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
