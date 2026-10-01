"""The coordinator as a service: an HTTP server process that workers talk to.

Usage: coordinator_service.py --registry FILE --jobs N [--host ADDR] [--port P]
                              [--heartbeat-timeout S] [--slow-complete S]

--host defaults to 127.0.0.1 (this machine only); use the LAN address, or 0.0.0.0
for every interface, to accept workers on other machines. --port 0 picks a free port.

Prints "READY <port>" once listening. Every request passes three checks before it
can touch state: the signature (worker identity), the protocol version, and the
message schema. State changes happen under one lock, so each request is applied
atomically: a check-then-act inside one request cannot interleave with another.

Failure detection: workers send heartbeats. A background thread marks a worker
*suspected* when nothing has been heard from it for longer than the timeout, and
reopens the job it held, so another worker can take it. A heartbeat from a
suspected worker revises the suspicion (the detector can be wrong: a slow worker
looks exactly like a dead one).

A completion from a worker that no longer owns the job is refused and recorded.
That protects the coordinator's own records; it cannot undo anything the worker
did elsewhere before reporting (see the README: zombies).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pydantic

from protocol import MESSAGES, VERSION, verify


def expected_result(job: str) -> str:
    return hashlib.sha256(job.encode()).hexdigest()[:12]


class State:
    def __init__(self, secrets: dict[str, str], n_jobs: int, timeout_s: float):
        self.lock = threading.Lock()
        self.secrets = secrets
        self.timeout_s = timeout_s
        self.jobs = {f"J{i}": {"status": "open", "owner": None, "result": None, "completions": 0}
                     for i in range(1, n_jobs + 1)}
        self.workers = {w: {"last_seen": None, "status": "unknown"} for w in secrets}
        self.events: list[dict] = []

    def event(self, **e):
        self.events.append({"t": round(time.time(), 3), **e})

    # --- request handlers (called with the lock held) -----------------------
    def heartbeat(self, w):
        info = self.workers[w]
        if info["status"] == "suspected":
            self.event(what="suspicion_revised", worker=w)
        info["last_seen"], info["status"] = time.time(), "alive"
        return {"ok": True}

    def claim(self, w):
        self.heartbeat(w)
        for jid, j in self.jobs.items():
            if j["status"] == "open":
                j["status"], j["owner"] = "claimed", w
                self.event(what="claim", worker=w, job=jid)
                return {"job": jid}
        return {"job": None, "all_done": all(j["status"] == "done" for j in self.jobs.values())}

    def complete(self, w, job, result):
        self.heartbeat(w)
        j = self.jobs.get(job)
        if j is None:
            return 404, {"error": "unknown job"}
        if j["status"] == "done" and j["owner"] == w and j["result"] == result:
            self.event(what="duplicate_complete_ignored", worker=w, job=job)
            return 200, {"ok": True, "duplicate": True}        # a retry: idempotent
        if j["owner"] != w or j["status"] != "claimed":
            self.event(what="refused_not_owner", worker=w, job=job, owner=j["owner"], status=j["status"])
            return 409, {"error": f"{w} does not own {job}"}
        j["status"], j["result"] = "done", result
        j["completions"] += 1
        self.event(what="complete", worker=w, job=job, correct=result == expected_result(job))
        return 200, {"ok": True}

    # --- failure detector (background thread) -------------------------------
    def detect(self):
        now = time.time()
        for w, info in self.workers.items():
            if info["status"] == "alive" and now - info["last_seen"] > self.timeout_s:
                info["status"] = "suspected"
                self.event(what="suspected", worker=w)
                for jid, j in self.jobs.items():
                    if j["owner"] == w and j["status"] == "claimed":
                        j["status"], j["owner"] = "open", None
                        self.event(what="reopened", job=jid, from_worker=w)

    def snapshot(self):
        return {"jobs": self.jobs, "workers": self.workers, "events": self.events}


def make_handler(state: State, slow_complete_s: float):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # keep stderr quiet
            pass

        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass                           # the client gave up waiting: this reply is lost

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"ok": True})
            if self.path == "/status":
                with state.lock:
                    return self.reply(200, state.snapshot())
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            model = MESSAGES.get(self.path)
            if model is None:
                return self.reply(404, {"error": "not found"})
            # 1. identity
            why = verify(state.secrets, self.headers.get("X-Worker", ""), self.headers.get("X-Timestamp", ""),
                         self.headers.get("X-Signature", ""), body)
            if why:
                with state.lock:
                    state.event(what="rejected_auth", claimed=self.headers.get("X-Worker"), reason=why)
                return self.reply(401, {"error": why})
            # 2. version, 3. schema
            try:
                raw = json.loads(body)
                if raw.get("v") != VERSION:
                    return self.reply(400, {"error": f"unsupported protocol version {raw.get('v')!r}"})
                msg = model.model_validate(raw)
            except (json.JSONDecodeError, AttributeError, pydantic.ValidationError) as e:
                return self.reply(400, {"error": f"malformed message: {type(e).__name__}"})
            if msg.worker != self.headers["X-Worker"]:
                return self.reply(401, {"error": "message worker differs from signer"})

            with state.lock:
                if self.path == "/heartbeat":
                    code, out = 200, state.heartbeat(msg.worker)
                elif self.path == "/claim":
                    code, out = 200, state.claim(msg.worker)
                else:
                    code, out = state.complete(msg.worker, msg.job, msg.result)
            if self.path == "/complete" and slow_complete_s and not out.get("duplicate"):
                time.sleep(slow_complete_s)    # processed, but the first reply is late: the client times out
            self.reply(code, out)
    return Handler


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # The base class resolves its own host name here (socket.getfqdn), which can
        # stall for tens of seconds on some hosts; the name is never used.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True, help="JSON file: worker id -> secret")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--heartbeat-timeout", type=float, default=0.6)
    ap.add_argument("--slow-complete", type=float, default=0.0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    state = State(json.load(open(a.registry)), a.jobs, a.heartbeat_timeout)

    def detector():
        while True:
            time.sleep(a.heartbeat_timeout / 4)
            with state.lock:
                state.detect()
    threading.Thread(target=detector, daemon=True).start()

    server = Server((a.host, a.port), make_handler(state, a.slow_complete))
    print(f"READY {server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
