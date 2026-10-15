"""A small signed-JSON HTTP service, shared by the ledger front-ends and the mail service.

Every POST passes four checks before it can touch state, in this order: the signature
(who is asking, checked with that participant's public key), the role (may that
participant ask for this at all), the protocol version, and the message schema.

A service given a key of its own also signs a receipt into every answer it gives to a
POST: evidence, in the participant's hands, of what this service told it.
"""

from __future__ import annotations

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pydantic

import sig
from wire import VERSION, receipt_doc, verify


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # The base class resolves its own host name here, which can stall for tens of
        # seconds on some hosts; the name is never used.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_handler(registry: dict, lock, posts: dict, gets: dict, slow_reply=None, signer=None):
    """posts: path -> (Model, roles, fn(msg) -> (code, dict)); gets: path -> fn() -> dict.
    slow_reply(path, out) -> seconds to wait before answering (fault injection).
    signer: (service name, private key) to sign receipts, or None."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
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
                pass                               # the client gave up waiting: this reply is lost

        def do_GET(self):
            fn = gets.get(self.path)
            if fn is None:
                return self.reply(404, {"error": "not found"})
            with lock:
                return self.reply(200, fn())

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            route = posts.get(self.path)
            if route is None:
                return self.reply(404, {"error": "not found"})
            model, roles, fn = route
            who = self.headers.get("X-Who", "")
            why = verify(registry, who, self.headers.get("X-Timestamp", ""),
                         self.headers.get("X-Signature", ""), body)
            if why:
                return self.reply(401, {"error": why})
            if registry[who]["role"] not in roles:
                return self.reply(403, {"error": f"a {registry[who]['role']} may not call {self.path}"})
            try:
                raw = json.loads(body)
                if raw.get("v") != VERSION:
                    return self.reply(400, {"error": f"unsupported protocol version {raw.get('v')!r}"})
                msg = model.model_validate(raw)
            except (json.JSONDecodeError, AttributeError, pydantic.ValidationError) as e:
                return self.reply(400, {"error": f"malformed message: {type(e).__name__}"})
            if msg.who != who:
                return self.reply(401, {"error": "message sender differs from signer"})
            with lock:
                code, out = fn(msg)
            if signer and code == 200:
                name, sk = signer
                out = {**out, "receipt": {"service": name, "status": code,
                                          "sig": sig.sign(sk, receipt_doc(name, sig.digest(body), code))}}
            delay = slow_reply(self.path, out) if slow_reply else 0
            if delay:
                time.sleep(delay)
            self.reply(code, out)

    return Handler


class NoLock:
    """For services that may serve requests concurrently (the store serialises writes)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serve(host: str, port: int, handler) -> Server:
    server = Server((host, port), handler)
    print(f"READY {server.server_address[1]}", flush=True)
    return server
