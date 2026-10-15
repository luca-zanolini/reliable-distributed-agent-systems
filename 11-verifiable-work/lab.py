"""Start the whole system on one host, with some parts lying, and observe.

The coordination plane is Stage 9's and 10's (three etcd members, two stateless front-ends).
Around it: workers, verifiers, reviewers, the committer, the repository, and the issuer.

Trust, as decided:
  - every participant has its own key pair; the front-ends hold public keys and roles only;
  - the repository is installed (by the operator: this lab) with the starting code and its
    pinned trust: the issuer's key, the verifiers' keys and policy, the reviewers' keys and
    kinds (human or bot);
  - the issuer signs each task: its title, its acceptance tests (pinned by content hash),
    and its review rules per path.

Requires etcd on the host (Homebrew: /opt/homebrew/opt/etcd/bin/etcd).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import evidence as E
import project
import sig
from etcd import Etcd
from wire import VERSION, AddTask, Client, get_json

HERE = Path(__file__).resolve().parent
ETCD = os.environ.get("ETCD_BIN", "/opt/homebrew/opt/etcd/bin/etcd")
MEMBERS = ("e1", "e2", "e3")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EtcdCluster:
    """Three etcd members as host processes (Stage 9's)."""

    def __init__(self, base: Path):
        self.base = base
        self.client = {m: free_port() for m in MEMBERS}
        self.peer = {m: free_port() for m in MEMBERS}
        self.procs: dict[str, subprocess.Popen] = {}
        cluster = ",".join(f"{m}=http://127.0.0.1:{self.peer[m]}" for m in MEMBERS)
        for m in MEMBERS:
            log = open(base / f"{m}.log", "a")
            self.procs[m] = subprocess.Popen(
                [ETCD, "--name", m, "--data-dir", str(base / m),
                 "--listen-client-urls", f"http://127.0.0.1:{self.client[m]}",
                 "--advertise-client-urls", f"http://127.0.0.1:{self.client[m]}",
                 "--listen-peer-urls", f"http://127.0.0.1:{self.peer[m]}",
                 "--initial-advertise-peer-urls", f"http://127.0.0.1:{self.peer[m]}",
                 "--initial-cluster", cluster, "--initial-cluster-state", "new",
                 "--initial-cluster-token", base.parent.name,
                 "--heartbeat-interval", "100", "--election-timeout", "1000"],
                stdout=log, stderr=log, env={**os.environ, "ETCD_UNSUPPORTED_ARCH": "arm64"})
            log.close()
        deadline = time.time() + 30
        e = Etcd([], timeout_s=0.5)
        while time.time() < deadline:
            views = [e.status(f"http://127.0.0.1:{self.client[m]}") for m in MEMBERS]
            leaders = {v["leader"] for v in views if v and v["leader"] and v["leader"] != "0"}
            if len(leaders) == 1:
                return
            time.sleep(0.1)
        raise TimeoutError("etcd elected no leader")

    def endpoints(self) -> list[str]:
        return [f"http://127.0.0.1:{self.client[m]}" for m in MEMBERS]

    def close(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                try:
                    os.kill(p.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                p.kill()
            p.wait()


def rules(fencing: str) -> list[dict]:
    return [{"prefix": "tests/", "review": "forbidden"}, {"prefix": "fencing.py", "review": fencing},
            {"prefix": "CHANGELOG.md", "review": "none"}]


class Lab:
    """workers: [(id, byzantine)]; verifiers: [(id, byzantine)]; reviewers: [(id, kind)];
    fencing_rule: the review the issuer requires for fencing.py (none | any | human);
    frontend_flags / repo_flags: extra arguments; worker_urls: "f1" to send workers to f1 only."""

    def __init__(self, tasks=("T1", "T2", "T3"), workers=(("w1", ""), ("w2", "")),
                 verifiers=(("v1", ""), ("v2", ""), ("v3", "")), reviewers=(("r1", "human"),),
                 policy=None, fencing_rule="any", lease_s=4.0, work_s=0.3, runs=3, frontend_flags=None,
                 repo_flags=(), worker_urls=None, worker_env=None):
        self.base = Path(tempfile.mkdtemp(prefix="verifiable-"))
        self.workers, self.verifiers, self.reviewers = list(workers), list(verifiers), list(reviewers)
        self.policy = policy or E.threshold(1)
        self.tasks, self.lease_s, self.work_s, self.runs = list(tasks), lease_s, work_s, runs
        ids = [w for w, _ in self.workers] + [v for v, _ in self.verifiers] + [r for r, _ in self.reviewers]
        self.ids = ids
        self.keys = {who: sig.keypair() for who in [*ids, "k1", "i1", "f1", "f2"]}
        roles = {**{w: "worker" for w, _ in self.workers}, **{v: "verifier" for v, _ in self.verifiers},
                 **{r: "reviewer" for r, _ in self.reviewers}, "k1": "committer", "i1": "issuer"}
        self.registry = {who: {"pk": self.keys[who][1], "role": role} for who, role in roles.items()}
        kinds = {r: k for r, k in self.reviewers}
        self.pinned = {"issuer": self.keys["i1"][1], "verifiers": {v: self.keys[v][1] for v, _ in self.verifiers},
                       "policy": self.policy, "reviewers": {r: {"pk": self.keys[r][1], "kind": k} for r, k in self.reviewers}}
        for d in ("etcd", "f1", "f2", "repo", *ids, "k1"):
            (self.base / d).mkdir()
        for f in ("f1", "f2"):
            (self.base / f / "registry.json").write_text(json.dumps(self.registry))
            (self.base / f / "policy.json").write_text(json.dumps(
                {"verifiers": [v for v, _ in self.verifiers], "reviewers": kinds, "policy": self.policy}))
            (self.base / f / "key").write_text(self.keys[f][0])
        (self.base / "repo" / "registry.json").write_text(json.dumps(self.registry))
        (self.base / "repo" / "pinned.json").write_text(json.dumps(self.pinned))
        (self.base / "repo" / "genesis.json").write_text(json.dumps(project.GENESIS))
        self.procs: dict[str, subprocess.Popen] = {}
        self.fe_port: dict[str, int] = {}
        self.cache: dict = {}

        self.etcd = EtcdCluster(self.base / "etcd")
        self.procs["repo"] = self._popen("repo", [HERE / "repo_service.py", "--registry", "registry.json", "--pinned",
                                                  "pinned.json", "--genesis", "genesis.json", "--data", ".", *repo_flags], {})
        self.repo_url = f"http://127.0.0.1:{self._ready(self.procs['repo'])}"
        self.genesis = get_json(self.repo_url + "/head")["head"]
        for f in ("f1", "f2"):
            cmd = [HERE / "frontend.py", "--registry", "registry.json", "--policy", "policy.json", "--key", "key",
                   "--etcd", ",".join(self.etcd.endpoints()), "--repo", self.repo_url, "--name", f,
                   "--lease", str(lease_s), *(frontend_flags or {}).get(f, [])]
            self.procs[f] = self._popen(f, cmd, {})
            self.fe_port[f] = int(self._ready(self.procs[f]))
        self.issue(fencing_rule)
        for i, (w, byz) in enumerate(self.workers):
            urls = self.fe_url(worker_urls) if worker_urls else self.ledger_urls(i)
            self.start(w, "worker", urls, BYZANTINE=byz, WORK_S=str(work_s), **(worker_env or {}).get(w, {}))
            time.sleep(0.15)                   # w1 first: it claims T1, the task the liars lie on
        for i, (v, byz) in enumerate(self.verifiers):
            self.start(v, "verifier", self.ledger_urls(i), BYZANTINE=byz, RUNS=str(runs), WORK_S="0.1")
        for i, (r, kind) in enumerate(self.reviewers):
            self.start(r, "reviewer", self.ledger_urls(i), KIND=kind, WORK_S="0.1")
        self.procs["k1"] = self._popen("k1", [HERE / "committer.py"], {
            "ID": "k1", "KEY": self.keys["k1"][0], "LEDGER_URLS": self.ledger_urls(0), "REPO_URL": self.repo_url})

    def fe_url(self, f: str) -> str:
        return f"http://127.0.0.1:{self.fe_port[f]}"

    def ledger_urls(self, rotate: int) -> str:
        fs = ["f1", "f2"][rotate % 2:] + ["f1", "f2"][:rotate % 2]
        return ",".join(self.fe_url(f) for f in fs)

    def issue(self, fencing_rule: str) -> None:
        issuer = Client(self.ledger_urls(1), "i1", self.keys["i1"][0], timeout_s=3.0, attempts=20)
        for tid in self.tasks:
            spec = project.TASKS[tid]
            body = {"title": spec["title"], "tests": {p: sig.digest(project.GENESIS[p]) for p in spec["tests"]},
                    "rules": rules(fencing_rule)}
            issuer.call("/task", AddTask(v=VERSION, who="i1", **E.issue(self.keys["i1"][0], tid, body)))

    def start(self, who, role, urls, **env):
        self.procs[who] = self._popen(who, [HERE / "agent.py"], {
            "ID": who, "ROLE": role, "KEY": self.keys[who][0], "LEDGER_URLS": urls, "REPO_URL": self.repo_url,
            **{k: v for k, v in env.items() if v}})

    # --- observing -----------------------------------------------------------------------------
    def status(self) -> dict | None:
        for f in ("f2", "f1"):
            if self.procs[f].poll() is None:
                s = get_json(self.fe_url(f) + "/status", timeout_s=3.0)
                if s and "tasks" in s:
                    return s
        return None

    def counters(self, f: str) -> dict:
        return (get_json(self.fe_url(f) + "/status", timeout_s=3.0) or {}).get("counters", {})

    def records(self, name: str) -> list[dict]:
        p = self.base / "repo" / name
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []

    def version(self, h: str) -> dict:
        if h in self.cache:
            return self.cache[h]
        return get_json(f"{self.repo_url}/version/{h}")["files"]

    def head(self) -> str:
        return self.cache.get("head") or get_json(self.repo_url + "/head")["head"]

    def journal(self, who) -> list[dict]:
        p = self.base / who / ("committer.jsonl" if who == "k1" else "agent.jsonl")
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def wait(self, predicate, timeout_s=60.0, what="condition"):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except Exception:
                pass
            time.sleep(0.1)
        raise TimeoutError(f"waiting for {what}")

    def close(self):
        """Keep what the checks need after shutdown: every committed version and the head."""
        try:
            for c in self.records("commits.jsonl"):
                for h in (c["base"], c["result"]):
                    self.cache[h] = self.version(h)
            self.cache["head"] = self.head()
            self.cache[self.cache["head"]] = self.version(self.cache["head"])
        except (OSError, TypeError, KeyError):
            pass
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()
            p.wait()
            if p.stdout:
                p.stdout.close()
        self.etcd.close()

    def _popen(self, who, cmd, env):
        return subprocess.Popen([sys.executable, *map(str, cmd)], cwd=self.base / who,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(HERE), **env},
                                stdout=subprocess.PIPE, text=True)

    def _ready(self, proc) -> str:
        line = proc.stdout.readline()
        if not line.startswith("READY"):
            raise RuntimeError(f"service did not start: {line!r}")
        return line.split()[1]
