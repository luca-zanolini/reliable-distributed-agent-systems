"""The outside world: a mail service whose deliveries cannot be taken back.

Usage: mail_service.py --registry FILE --deliveries FILE [--host ADDR] [--port P]
                       [--no-dedupe] [--slow-first-reply S]

Only the sender is in its registry, so only the sender can ask it to deliver. In the
lab it also sits on its own network, which workers are not attached to: they could
not reach it even with a stolen secret.

Each delivery carries an idempotency key. The service remembers every key it has
delivered, durably (the deliveries file, appended and forced to disk before
answering), and answers a repeated key with "duplicate" instead of delivering again.
The deliveries file is the ground truth for the experiments: one line per email that
actually went out.

  --no-dedupe            deliver every request, repeats included (to show why the
                         receiver's memory matters)
  --slow-first-reply S   deliver the first email, then hold the reply S seconds, so
                         the sender times out without knowing whether it was sent
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

from service import make_handler, serve
from wire import Deliver


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--deliveries", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-dedupe", action="store_true")
    ap.add_argument("--slow-first-reply", type=float, default=0.0)
    a = ap.parse_args()
    registry = json.loads(Path(a.registry).read_text())
    path = Path(a.deliveries)
    path.touch()
    delivered = {json.loads(line)["key"] for line in path.read_text().splitlines() if line.strip()}
    suppressed = []

    def deliver(m: Deliver):
        if m.key in delivered and not a.no_dedupe:
            suppressed.append(m.key)
            return 200, {"duplicate": True}
        with open(path, "a") as f:                 # the irreversible act, recorded before answering
            f.write(json.dumps({"key": m.key, "to": m.to, "subject": m.subject, "text": m.text}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        delivered.add(m.key)
        return 200, {"delivered": True}

    def status():
        lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return {"deliveries": [d["key"] for d in lines], "suppressed": list(suppressed)}

    slowed = []

    def slow_reply(path_, out):
        if a.slow_first_reply and not slowed and out.get("delivered"):
            slowed.append(True)
            return a.slow_first_reply
        return 0

    handler = make_handler(registry, threading.Lock(), {"/deliver": (Deliver, {"sender"}, deliver)},
                           {"/health": lambda: {"ok": True}, "/status": status}, slow_reply)
    serve(a.host, a.port, handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
