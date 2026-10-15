"""Start the whole system, with some of its parts lying, and observe.

The coordination plane is Stage 9's: three etcd members (a real Raft cluster) and two
stateless front-ends f1, f2. Around it: workers, checkers, a sender, the mail service,
and the issuer that creates the tasks. Every process is an OS process on one host (the
attacks of this stage are about forged evidence, not about who can reach whom; the
network isolation of Stage 9 is unchanged and not re-tested here).

Keys and trust, as decided (D7):
  - every participant, the issuer and each front-end has its own key pair; the lab writes
    each private key only into its owner's directory or environment;
  - the front-ends get the participants' PUBLIC keys and roles (registry.json);
  - the mail service is installed with its pinned trust (pinned.json): the issuer's public
    key, the checkers' public keys, and the approval policy. Nothing it receives later can
    change that file.

Any front-end, worker, checker or the sender can be made Byzantine by configuration.

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
        self.cluster = ",".join(f"{m}=http://127.0.0.1:{self.peer[m]}" for m in MEMBERS)
        for m in MEMBERS:
            self.start(m)
        self.wait_leader()

    def endpoints(self) -> list[str]:
        return [f"http://127.0.0.1:{self.client[m]}" for m in MEMBERS]

    def start(self, m: str) -> None:
        state = "existing" if (self.base / m / "member").exists() else "new"
        log = open(self.base / f"{m}.log", "a")
        self.procs[m] = subprocess.Popen(
            [ETCD, "--name", m, "--data-dir", str(self.base / m),
             "--listen-client-urls", f"http://127.0.0.1:{self.client[m]}",
             "--advertise-client-urls", f"http://127.0.0.1:{self.client[m]}",
             "--listen-peer-urls", f"http://127.0.0.1:{self.peer[m]}",
             "--initial-advertise-peer-urls", f"http://127.0.0.1:{self.peer[m]}",
             "--initial-cluster", self.cluster, "--initial-cluster-state", state,
             "--initial-cluster-token", self.base.parent.name,
             "--heartbeat-interval", "100", "--election-timeout", "1000"],
            stdout=log, stderr=log, env={**os.environ, "ETCD_UNSUPPORTED_ARCH": "arm64"})
        log.close()

    def leader(self) -> str | None:
        e = Etcd([], timeout_s=0.5)
        views = {m: e.status(f"http://127.0.0.1:{self.client[m]}") for m in MEMBERS}
        ids = {v["member"]: m for m, v in views.items() if v}
        leaders = {v["leader"] for v in views.values() if v and v["leader"] and v["leader"] != "0"}
        return ids.get(leaders.pop()) if len(leaders) == 1 else None

    def wait_leader(self, timeout_s=30.0) -> str:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            m = self.leader()
            if m:
                return m
            time.sleep(0.1)
        raise TimeoutError("etcd elected no leader")

    def close(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                try:
                    os.kill(p.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                p.kill()
            p.wait()


class Lab:
    """workers: [(id, byzantine)]; checkers: [(id, kind, byzantine)]; policy: evidence.threshold(...)
    or evidence.fail_prone(...); frontend_flags: {"f1": [...], "f2": [...]};
    checker_urls: "f1" to send every checker to f1 only (default: both, rotated);
    agent_env: {who: {VAR: value}} to override one agent's environment (e.g. its speed)."""

    def __init__(self, workers=(("w1", ""), ("w2", ""), ("w3", "")),
                 checkers=(("c1", "rule", ""), ("c2", "rule", ""), ("c3", "rule", "")),
                 policy=None, tasks=8, lease_s=3.0, work_s=0.4, frontend_flags=None, mail_flags=(),
                 sender_env=None, checker_urls=None, agent_env=None):
        self.lease_s, self.work_s, self.n_tasks = lease_s, work_s, tasks
        self.base = Path(tempfile.mkdtemp(prefix="byzantine-"))
        self.workers, self.checkers = list(workers), list(checkers)
        self.policy = policy or E.threshold(1)
        self.ids = [w for w, _ in self.workers] + [c for c, _, _ in self.checkers]
        self.keys = {who: sig.keypair() for who in [*self.ids, "s1", "i1", "f1", "f2"]}
        roles = {**{w: "worker" for w, _ in self.workers}, **{c: "checker" for c, _, _ in self.checkers},
                 "s1": "sender", "i1": "issuer"}
        self.registry = {who: {"pk": self.keys[who][1], "role": role} for who, role in roles.items()}
        checker_ids = [c for c, _, _ in self.checkers]
        self.pinned = {"issuer": self.keys["i1"][1], "checkers": {c: self.keys[c][1] for c in checker_ids},
                       "policy": self.policy}
        for d in ("etcd", "f1", "f2", "mail", *self.ids, "s1"):
            (self.base / d).mkdir()
        for f in ("f1", "f2"):
            (self.base / f / "registry.json").write_text(json.dumps(self.registry))
            (self.base / f / "policy.json").write_text(json.dumps({"checkers": checker_ids, "policy": self.policy}))
            (self.base / f / "key").write_text(self.keys[f][0])
        (self.base / "mail" / "registry.json").write_text(json.dumps({"s1": self.registry["s1"]}))
        (self.base / "mail" / "pinned.json").write_text(json.dumps(self.pinned))
        self.procs: dict[str, subprocess.Popen] = {}
        self.frontend_flags = frontend_flags or {}
        self.fe_port: dict[str, int] = {}
        self.checker_urls = checker_urls
        self.agent_env = agent_env or {}

        self.etcd = EtcdCluster(self.base / "etcd")
        for f in ("f1", "f2"):
            self.start_frontend(f)
        self.issue_tasks()
        self.procs["mail"] = self._popen("mail", [HERE / "mail_service.py", "--registry", "registry.json",
                                                  "--pinned", "pinned.json", "--deliveries", "deliveries.jsonl",
                                                  *mail_flags], {})
        self.mail_url = f"http://127.0.0.1:{self._ready(self.procs['mail'])}"
        self.wait(lambda: get_json(self.mail_url + "/health"), 30, "mail service")
        for i, (w, byz) in enumerate(self.workers):
            self.start_agent(w, i, ROLE="worker", BYZANTINE=byz)
        for i, (c, kind, byz) in enumerate(self.checkers):
            self.start_agent(c, i, ROLE="checker", KIND=kind, BYZANTINE=byz)
        self.start_sender(sender_env or {})

    # --- the coordination plane ----------------------------------------------------------
    def fe_url(self, f: str) -> str:
        return f"http://127.0.0.1:{self.fe_port[f]}"

    def start_frontend(self, f: str) -> None:
        cmd = [HERE / "frontend.py", "--registry", "registry.json", "--policy", "policy.json", "--key", "key",
               "--etcd", ",".join(self.etcd.endpoints()), "--name", f, "--lease", str(self.lease_s),
               "--port", str(self.fe_port.get(f, 0)), *self.frontend_flags.get(f, [])]
        self.procs[f] = self._popen(f, cmd, {})
        self.fe_port[f] = int(self._ready(self.procs[f]))

    def ledger_urls(self, rotate: int) -> str:
        fs = ["f1", "f2"]
        fs = fs[rotate % 2:] + fs[:rotate % 2]
        return ",".join(self.fe_url(f) for f in fs)

    def issue_tasks(self) -> None:
        """The issuer signs each task and hands it to the ledger (through the honest-by-default f2)."""
        issuer = Client(self.ledger_urls(1), "i1", self.keys["i1"][0], timeout_s=3.0, attempts=20)
        for i in range(1, self.n_tasks + 1):
            to = "not-an-address" if i == self.n_tasks else f"customer{i}@example.com"
            st = E.issue(self.keys["i1"][0], f"T{i}", {"to": to, "subject": f"invoice #{4400 + i}"})
            issuer.call("/task", AddTask(v=VERSION, who="i1", **st))

    # --- participants ----------------------------------------------------------------------
    def start_agent(self, who, rotate=0, **env):
        urls = self.fe_url(self.checker_urls) if env.get("ROLE") == "checker" and self.checker_urls \
            else self.ledger_urls(rotate)
        work = self.work_s if env.get("ROLE") == "worker" else self.work_s / 4
        self.procs[who] = self._popen(who, [HERE / "agent.py"], {"ID": who, "KEY": self.keys[who][0],
                                                                "LEDGER_URLS": urls, "WORK_S": str(work), **env,
                                                                **self.agent_env.get(who, {})})

    def start_sender(self, extra: dict):
        env = {"ID": "s1", "KEY": self.keys["s1"][0], "LEDGER_URLS": self.ledger_urls(0),
               "MAIL_URL": self.mail_url, **extra}
        self.procs["s1"] = self._popen("s1", [HERE / "sender.py"], env)

    def exited(self, who) -> bool:
        return self.procs[who].poll() is not None

    # --- observing -------------------------------------------------------------------------
    def status(self) -> dict | None:
        for f in ("f2", "f1"):
            if f in self.fe_port and self.procs[f].poll() is None:
                s = get_json(self.fe_url(f) + "/status", timeout_s=3.0)
                if s and "tasks" in s:
                    return s
        return None

    def counters(self, f: str) -> dict:
        return (get_json(self.fe_url(f) + "/status", timeout_s=3.0) or {}).get("counters", {})

    def mail(self) -> dict | None:
        return get_json(self.mail_url + "/status")

    def deliveries(self) -> list[dict]:
        path = self.base / "mail" / "deliveries.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def journal(self, who) -> list[dict]:
        name = "sender.jsonl" if who == "s1" else "agent.jsonl"
        path = self.base / who / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def grants(self) -> list[tuple[str, int, str]]:
        return [(e["task"], e["token"], w) for w, _ in self.workers for e in self.journal(w)
                if e.get("state") == "claimed"]

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
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()
            p.wait()
            if p.stdout:
                p.stdout.close()
        self.etcd.close()

    # --- plumbing ----------------------------------------------------------------------------
    def _popen(self, who, cmd, env):
        return subprocess.Popen([sys.executable, *map(str, cmd)], cwd=self.base / who,
                                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(HERE), **env},
                                stdout=subprocess.PIPE, text=True)

    def _ready(self, proc) -> str:
        line = proc.stdout.readline()
        if not line.startswith("READY"):
            raise RuntimeError(f"service did not start: {line!r}")
        return line.split()[1]
