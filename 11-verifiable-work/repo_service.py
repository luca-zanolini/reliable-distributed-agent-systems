"""The repository: where the code lives, and the gate before anything becomes the current code.

Usage: repo_service.py --registry FILE --pinned FILE --genesis FILE --data DIR [--host H] [--port P]
                       [--no-base-check]

What it holds (Stage 6's repository, as a service):
  versions   every version ever stored, named by the hash of its files; storing a version is
             harmless (it changes nothing that anyone relies on), so any participant may store
  head       the current code: one version

Reads (GET, unauthenticated):  /head  /version/<hash>  /blob/<hash>  /diff?base=..&result=..  /status
Writes (POST, signed):          /store {files}     /commit {signed_task, base, result, attestations, reviews}

/commit is the Stage 11 counterpart of Stage 10's mail service. The repository works out by
itself which paths the change touches (from the two stored versions) and runs
evidence.check_commit against its pinned trust; then it moves the head only if the change was
made on top of the current head (a compare-and-swap: base must equal head). Anyone may ask: a
request carries evidence, and the evidence is what counts. Every commit and every refusal is
recorded, with its evidence, in commits.jsonl and refusals.jsonl (the ground truth for the
experiments).

  --no-base-check   the counterfactual: accept a certified version built on an older head
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pydantic

import evidence as E
import sig
from service import Server
from wire import VERSION, Commit, Store, verify


class Repository:
    def __init__(self, data: Path, pinned: dict, genesis: dict, base_check: bool = True):
        self.data, self.pinned, self.base_check = data, pinned, base_check
        self.versions: dict[str, dict] = {}
        self.blobs: dict[str, str] = {}
        self.lock = threading.Lock()
        self.head = self.store(genesis)
        self.history = [self.head]
        self.committed: set[tuple[str, str]] = set()   # (task, version): a retried commit is recognised
        for name in ("commits.jsonl", "refusals.jsonl"):
            (data / name).touch()

    def store(self, files: dict) -> str:
        h = E.version_hash(files)
        self.versions[h] = dict(files)
        for content in files.values():
            self.blobs[sig.digest(content)] = content
        return h

    def _record(self, name: str, entry: dict) -> None:
        with open(self.data / name, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def commit(self, c: Commit, who: str) -> tuple[int, dict]:
        if (c.signed_task.get("task"), c.result) in self.committed:
            return 200, {"committed": True, "duplicate": True, "head": self.head}
        base, result = self.versions.get(c.base), self.versions.get(c.result)
        if base is None or result is None:
            why = "unknown version"
        else:
            changed = E.changed_paths(base, result)
            why = E.check_commit(self.pinned, c.signed_task, c.base, c.result, changed, c.attestations, c.reviews)
            if why is None and self.base_check and c.base != self.head:
                why = f"stale base: the change was made on {c.base[:12]}, the current code is {self.head[:12]}"
        entry = {"task": c.signed_task.get("task"), "by": who, "base": c.base, "result": c.result,
                 "previous_head": self.head, "attestations": c.attestations, "reviews": c.reviews,
                 "signed_task": c.signed_task}
        if why:
            self._record("refusals.jsonl", {**entry, "reason": why})
            return 422, {"error": why}
        self.head = c.result
        self.history.append(c.result)
        self.committed.add((c.signed_task.get("task"), c.result))
        self._record("commits.jsonl", {**entry, "changed": changed})
        return 200, {"committed": True, "head": self.head}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--pinned", required=True)
    ap.add_argument("--genesis", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-base-check", action="store_true")
    a = ap.parse_args()
    registry = json.loads(Path(a.registry).read_text())
    repo = Repository(Path(a.data), json.loads(Path(a.pinned).read_text()),
                      json.loads(Path(a.genesis).read_text()), base_check=not a.no_base_check)

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
                pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            parts = url.path.strip("/").split("/")
            with repo.lock:
                if url.path == "/head":
                    return self.reply(200, {"head": repo.head})
                if url.path in ("/health",):
                    return self.reply(200, {"ok": True})
                if url.path == "/status":
                    return self.reply(200, {"head": repo.head, "history": repo.history})
                if parts[0] == "version" and len(parts) == 2 and parts[1] in repo.versions:
                    return self.reply(200, {"files": repo.versions[parts[1]]})
                if parts[0] == "blob" and len(parts) == 2 and parts[1] in repo.blobs:
                    return self.reply(200, {"content": repo.blobs[parts[1]]})
                if url.path == "/diff":
                    b, r = repo.versions.get(q.get("base", [""])[0]), repo.versions.get(q.get("result", [""])[0])
                    if b is None or r is None:
                        return self.reply(404, {"error": "unknown version"})
                    return self.reply(200, {"changed": E.changed_paths(b, r)})
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            who = self.headers.get("X-Who", "")
            why = verify(registry, who, self.headers.get("X-Timestamp", ""), self.headers.get("X-Signature", ""), body)
            if why:
                return self.reply(401, {"error": why})
            model = {"/store": Store, "/commit": Commit}.get(self.path)
            if model is None:
                return self.reply(404, {"error": "not found"})
            try:
                raw = json.loads(body)
                if raw.get("v") != VERSION:
                    return self.reply(400, {"error": "unsupported protocol version"})
                msg = model.model_validate(raw)
            except (json.JSONDecodeError, AttributeError, pydantic.ValidationError) as e:
                return self.reply(400, {"error": f"malformed message: {type(e).__name__}"})
            if msg.who != who:
                return self.reply(401, {"error": "message sender differs from signer"})
            with repo.lock:
                if self.path == "/store":
                    return self.reply(200, {"version": repo.store(msg.files)})
                code, out = repo.commit(msg, who)
            self.reply(code, out)

    server = Server((a.host, a.port), Handler)
    print(f"READY {server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
