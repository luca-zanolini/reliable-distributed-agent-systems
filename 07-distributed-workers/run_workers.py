"""Run workers on this machine against a coordinator on another one.

  python run_workers.py new-registry FILE
      Create a registry of worker secrets. Copy it to the coordinator's machine over an
      encrypted channel (scp); never by e-mail or chat.

  python run_workers.py run --registry FILE --url http://HOST:PORT [--work S] [--heartbeat S]
      Start one worker process per registry entry, each in its own sandbox directory with
      only its own secret, and report progress until every job is done.

The coordinator must be started with the same registry and listen on a LAN address:
  coordinator_service.py --registry FILE --jobs N --host 0.0.0.0 --port 8080 --heartbeat-timeout 2
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def new_registry(path: str, n: int = 3) -> None:
    Path(path).write_text(json.dumps({f"w{i}": secrets.token_hex(16) for i in range(1, n + 1)}, indent=2))
    Path(path).chmod(0o600)                       # readable by its owner only
    print(f"wrote {n} worker secrets to {path} (mode 600)")


def status(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/status", timeout=1) as r:
            return json.loads(r.read())
    except OSError:
        return None                               # coordinator unreachable from here


def run(registry: str, url: str, work_s: float, heartbeat_s: float) -> None:
    base = Path(tempfile.mkdtemp(prefix="workers-"))
    log = base / "external-effects.log"
    procs = {}
    for w, secret in json.loads(Path(registry).read_text()).items():
        (base / w).mkdir()
        env = {"PATH": "/usr/bin:/bin", "WORKER_ID": w, "WORKER_SECRET": secret, "COORD_URL": url,
               "WORK_SECONDS": str(work_s), "HEARTBEAT_S": str(heartbeat_s), "EXTERNAL_LOG": str(log)}
        procs[w] = subprocess.Popen([sys.executable, str(HERE / "worker.py")], cwd=base / w, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"started {list(procs)} (sandboxes under {base}); coordinator {url}")

    seen, t0 = 0, time.time()
    while True:
        s = status(url)
        if s is None:
            print(f"  {time.time() - t0:6.1f}s  coordinator UNREACHABLE")
        else:
            for e in s["events"][seen:]:
                if e["what"] in ("suspected", "reopened", "suspicion_revised", "refused_not_owner", "complete"):
                    who = e.get("worker") or e.get("from_worker", "")
                    print(f"  {time.time() - t0:6.1f}s  {e['what']:<18} {who:<4} {e.get('job', '')}")
            seen = len(s["events"])
            if all(j["status"] == "done" for j in s["jobs"].values()):
                break
        time.sleep(0.5)

    for p in procs.values():
        p.wait(timeout=10)
    effects = Counter(line.split()[0] for line in log.read_text().splitlines()) if log.exists() else Counter()
    ev = Counter(e["what"] for e in s["events"])
    print("\nall jobs done")
    print(f"  completions per job      {sorted(Counter(j['completions'] for j in s['jobs'].values()).items())}  ([(count, jobs)])")
    print(f"  suspicions / revisions   {ev['suspected']} / {ev['suspicion_revised']}")
    print(f"  jobs reopened            {ev['reopened']}")
    print(f"  zombie reports refused   {ev['refused_not_owner']}")
    print(f"  external effects         {sum(effects.values())} for {len(effects)} jobs; "
          f"duplicated: {sorted(j for j, n in effects.items() if n > 1) or 'none'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new-registry")
    n.add_argument("file")
    r = sub.add_parser("run")
    r.add_argument("--registry", required=True)
    r.add_argument("--url", required=True)
    r.add_argument("--work", type=float, default=1.0)
    r.add_argument("--heartbeat", type=float, default=0.3)
    a = ap.parse_args()
    new_registry(a.file) if a.cmd == "new-registry" else run(a.registry, a.url, a.work, a.heartbeat)
