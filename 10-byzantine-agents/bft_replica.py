"""One replica of the BFT ledger (experiment 9): the ledger's claim rule, applied in order
to the requests that a CometBFT network has agreed on.

Usage: bft_replica.py --rpc URL --registry FILE --tasks N [--port P] [--lie]

CometBFT orders requests (Byzantine fault-tolerant consensus among 3f+1 nodes, here with
its built-in key-value application, used only as a log). This process reads the
committed blocks of its own node, one height after another, and applies each request to
its own copy of the state with the same deterministic rule. Every honest replica reads
the same blocks in the same order, so every honest replica computes the same answers:
state-machine replication [Sch90], with consensus that tolerates lying nodes.

A request is a claim signed by a worker. A replica applies only requests whose signature
verifies against the worker's public key, so whoever proposes a block (any node, honest or
not) cannot slip in a claim in someone else's name.

It answers GET /grant?rid=... with what that claim obtained (or "pending" if the claim is
not committed yet) and GET /state with the height it has applied and a hash of its state.

  --lie   this replica answers every query with "you hold T1 with token 1", whatever the
          log says: a Byzantine replica. Its consensus votes stay honest (CometBFT's own
          tests cover lying votes); what it lies about is the ledger's answer.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sig


def claim_doc(who: str, rid: str) -> dict:
    return {"kind": "claim", "who": who, "rid": rid}


def encode(request: dict) -> str:
    """A request as a CometBFT key-value transaction: '<rid>=<hex of the signed request>'."""
    return f"{request['rid']}={sig.canon(request).hex()}"


def decode(tx: bytes) -> dict | None:
    try:
        _, hexed = tx.decode().split("=", 1)
        return json.loads(bytes.fromhex(hexed))
    except (ValueError, UnicodeDecodeError):
        return None


class Replica:
    def __init__(self, registry: dict, tasks: int):
        self.registry = registry
        self.tasks = {f"T{i}": {"state": "OPEN", "epoch": 0, "owner": None} for i in range(1, tasks + 1)}
        self.answers: dict[str, dict] = {}
        self.height = 0
        self.ignored = 0
        self.lock = threading.Lock()

    def apply(self, request: dict) -> None:
        """The ledger's claim rule (ledger.claimed), deterministic, on a verified request."""
        who, rid = request.get("who"), request.get("rid")
        entry = self.registry.get(who)
        if not entry or entry["role"] != "worker" or not sig.verify(entry["pk"], claim_doc(who, rid), request.get("sig", "")):
            self.ignored += 1                      # forged or malformed: no effect, on every honest replica
            return
        if rid in self.answers:
            return                                  # a repeated request: the answer stands
        tid = next((t for t in sorted(self.tasks, key=lambda t: (len(t), t)) if self.tasks[t]["state"] == "OPEN"), None)
        if tid is None:
            self.answers[rid] = {"task": None}
            return
        t = self.tasks[tid]
        t.update(state="IN_PROGRESS", epoch=t["epoch"] + 1, owner=who)
        self.answers[rid] = {"task": tid, "token": t["epoch"], "holder": who}

    def state_hash(self) -> str:
        return sig.digest(sig.canon({"tasks": self.tasks, "answers": self.answers}))


def follow(replica: Replica, rpc: str, stop: threading.Event) -> None:
    """Apply committed blocks, in height order, forever."""
    while not stop.is_set():
        try:
            with urllib.request.urlopen(f"{rpc}/block?height={replica.height + 1}", timeout=2) as r:
                block = json.load(r)
        except OSError:
            time.sleep(0.05)
            continue
        if "result" not in block:
            time.sleep(0.05)                        # that height does not exist yet
            continue
        txs = block["result"]["block"]["data"]["txs"] or []
        with replica.lock:
            for tx in txs:
                req = decode(base64.b64decode(tx))
                if req is not None:
                    replica.apply(req)
            replica.height += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--tasks", type=int, default=3)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--lie", action="store_true")
    a = ap.parse_args()
    replica = Replica(json.loads(Path(a.registry).read_text()), a.tasks)
    stop = threading.Event()
    threading.Thread(target=follow, args=(replica, a.rpc, stop), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            with replica.lock:
                if url.path == "/grant":
                    rid = q.get("rid", [""])[0]
                    out = {"task": "T1", "token": 1} if a.lie else replica.answers.get(rid, {"pending": True})
                elif url.path == "/state":
                    out = {"height": replica.height, "hash": replica.state_hash(), "ignored": replica.ignored,
                           "tasks": replica.tasks}
                else:
                    out = {"error": "not found"}
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"READY {server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
