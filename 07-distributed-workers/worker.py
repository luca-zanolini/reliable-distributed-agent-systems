"""A worker process: heartbeats, claims jobs, does the work, reports back.

Configured entirely by its environment (it is started with nothing else):
  WORKER_ID, WORKER_SECRET   its identity, known only to it and the coordinator
  COORD_URL                  where the coordinator listens
  WORK_SECONDS, HEARTBEAT_S  how long a job takes; how often to signal liveness
  EXTERNAL_LOG               an external system the work affects directly

Its working directory is its private sandbox: it keeps a local journal there
(journal.jsonl). The external log stands for any system outside the coordinator's
control (an email service, a payment API): writing to it is the job's side effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from protocol import VERSION, Claim, Complete, Heartbeat
from rpc import Client, RPCError

WORKER = os.environ["WORKER_ID"]
client = Client(os.environ["COORD_URL"], WORKER, os.environ["WORKER_SECRET"])


def journal(**entry):
    with open("journal.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def heartbeats(interval_s: float):
    while True:
        try:
            client.call("/heartbeat", Heartbeat(v=VERSION, worker=WORKER))
        except Exception:
            pass                                   # the coordinator may be briefly unreachable
        time.sleep(interval_s)


def main():
    threading.Thread(target=heartbeats, args=(float(os.environ["HEARTBEAT_S"]),), daemon=True).start()
    work_s = float(os.environ["WORK_SECONDS"])
    while True:
        try:
            reply = client.call("/claim", Claim(v=VERSION, worker=WORKER))
        except OSError:                                            # coordinator unreachable: wait, retry
            journal(state="coordinator_unreachable")
            time.sleep(1)
            continue
        job = reply.get("job")
        if job is None:
            if reply.get("all_done"):
                journal(state="exit")
                return
            time.sleep(0.05)
            continue
        journal(job=job, state="started")
        time.sleep(work_s)                                         # the work itself
        result = hashlib.sha256(job.encode()).hexdigest()[:12]
        with open(os.environ["EXTERNAL_LOG"], "a") as f:          # the side effect, outside the coordinator
            f.write(f"{job} {WORKER}\n")
        journal(job=job, state="finished", result=result)
        try:
            client.call("/complete", Complete(v=VERSION, worker=WORKER, job=job, result=result))
            journal(job=job, state="reported")
        except RPCError as e:
            journal(job=job, state="refused", status=e.status)
        except OSError as e:                                       # every retry failed: outcome unknown
            journal(job=job, state="unreported", error=type(e).__name__)


if __name__ == "__main__":
    main()
