"""Start a coordinator and workers as separate OS processes, and inject failures.

Each process gets its own sandbox directory as working directory and a minimal
environment: the coordinator receives the registry of worker secrets; each worker
receives only its own secret. Failures are real signals: SIGKILL (crash),
SIGSTOP/SIGCONT (freeze and resume, as a long pause or a partition would).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Cluster:
    def __init__(self, n_workers=3, n_jobs=6, work_s=0.3, heartbeat_s=0.1, timeout_s=0.6,
                 slow_complete_s=0.0):
        self.base = Path(tempfile.mkdtemp(prefix="cluster-"))
        self.external_log = self.base / "external" / "effects.log"
        self.external_log.parent.mkdir()
        self.secrets = {f"w{i}": secrets.token_hex(16) for i in range(1, n_workers + 1)}
        coord_dir = self.base / "coordinator"
        coord_dir.mkdir()
        (coord_dir / "registry.json").write_text(json.dumps(self.secrets))
        self.coord = subprocess.Popen(
            [sys.executable, str(HERE / "coordinator_service.py"), "--registry", "registry.json",
             "--jobs", str(n_jobs), "--heartbeat-timeout", str(timeout_s), "--slow-complete", str(slow_complete_s)],
            cwd=coord_dir, env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE, text=True)
        self.url = f"http://127.0.0.1:{self.coord.stdout.readline().split()[1]}"
        self.workers = {}
        for w, secret in self.secrets.items():
            sandbox = self.base / w
            sandbox.mkdir()
            env = {"PATH": "/usr/bin:/bin", "WORKER_ID": w, "WORKER_SECRET": secret, "COORD_URL": self.url,
                   "WORK_SECONDS": str(work_s), "HEARTBEAT_S": str(heartbeat_s),
                   "EXTERNAL_LOG": str(self.external_log)}
            self.workers[w] = subprocess.Popen([sys.executable, str(HERE / "worker.py")], cwd=sandbox, env=env,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- observation ----------------------------------------------------------
    def status(self) -> dict:
        with urllib.request.urlopen(self.url + "/status", timeout=2) as r:
            return json.loads(r.read())

    def wait_until(self, predicate, timeout_s=15.0, interval_s=0.02):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            s = self.status()
            if predicate(s):
                return s
            time.sleep(interval_s)
        raise TimeoutError("condition not reached")

    def all_done(self, s=None) -> bool:
        s = s or self.status()
        return all(j["status"] == "done" for j in s["jobs"].values())

    def effects(self) -> list[str]:
        return self.external_log.read_text().splitlines() if self.external_log.exists() else []

    def journal(self, w) -> list[dict]:
        p = self.base / w / "journal.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    # --- failure injection ----------------------------------------------------
    def kill(self, w):
        self.workers[w].send_signal(signal.SIGKILL)
        self.workers[w].wait()

    def freeze(self, w):
        self.workers[w].send_signal(signal.SIGSTOP)

    def resume(self, w):
        self.workers[w].send_signal(signal.SIGCONT)

    def close(self):
        for p in [*self.workers.values(), self.coord]:
            if p.poll() is None:
                p.send_signal(signal.SIGCONT)
                p.kill()
                p.wait()
        self.coord.stdout.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
