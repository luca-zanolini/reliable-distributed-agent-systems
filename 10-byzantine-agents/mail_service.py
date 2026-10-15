"""The outside world's gatekeeper: delivers only what the evidence covers.

Usage: mail_service.py --registry FILE --pinned FILE --deliveries FILE [--host ADDR] [--port P]
                       [--trust-supplied-keys]

Stage 9's mail service delivered whatever the sender asked. This one checks, by itself,
the evidence that comes with each request (evidence.check_delivery):

  - the task is signed by the issuer whose public key was installed with this service;
  - the text is the one the verdicts are about (it hashes the text itself);
  - the approving verdicts are signed by pinned checkers and satisfy the pinned policy.

It delivers to the address in the signed task, never to one the sender names, and keeps
Stage 8's idempotency record (one delivery per task id). It believes nothing the sender
or the ledger says: its pinned file is the only trust it has, and it is installed by the
operator, not received over the wire.

Every refusal is recorded with its reason. The deliveries file is the ground truth for the
experiments: one line per email that actually went out, with its evidence.

  --trust-supplied-keys   the counterfactual: accept a checker's public key carried inside
                          the certificate itself, as if the key list came from the ledger
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

import evidence as E
from service import make_handler, serve
from wire import Deliver


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--pinned", required=True)
    ap.add_argument("--deliveries", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--trust-supplied-keys", action="store_true")
    a = ap.parse_args()
    registry = json.loads(Path(a.registry).read_text())
    pinned = json.loads(Path(a.pinned).read_text())
    path = Path(a.deliveries)
    path.touch()
    delivered = {json.loads(line)["key"] for line in path.read_text().splitlines() if line.strip()}
    refusals, suppressed = [], []

    def deliver(m: Deliver):
        why = E.check_delivery(pinned, m.signed_task, m.text, m.certificate, a.trust_supplied_keys)
        if why is None and m.key != m.signed_task.get("task"):
            why = "idempotency key does not name the task"
        if why:
            refusals.append({"key": m.key, "reason": why, "text": m.text[:80]})
            return 422, {"error": why}
        if m.key in delivered:
            suppressed.append(m.key)
            return 200, {"duplicate": True}
        record = {"key": m.key, "to": m.signed_task["body"]["to"], "subject": m.signed_task["body"]["subject"],
                  "text": m.text, "signed_task": m.signed_task, "certificate": m.certificate}
        with open(path, "a") as f:                 # the irreversible act, recorded before answering
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        delivered.add(m.key)
        return 200, {"delivered": True}

    def status():
        lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return {"deliveries": [d["key"] for d in lines], "suppressed": list(suppressed),
                "refusals": list(refusals)}

    handler = make_handler(registry, threading.Lock(), {"/deliver": (Deliver, {"sender"}, deliver)},
                           {"/health": lambda: {"ok": True}, "/status": status})
    serve(a.host, a.port, handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
