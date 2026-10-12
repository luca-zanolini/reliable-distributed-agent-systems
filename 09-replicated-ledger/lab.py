"""Start the whole system, inject failures, and observe.

The coordination plane is replicated now:
  etcd       three members (e1, e2, e3), host processes, a real Raft cluster; any one of
             them can crash or freeze and the plane keeps deciding
  front-ends two (f1, f2), host processes, stateless: the ledger's rules over etcd
Everything else is Stage 8's, with two backends:
  proc       workers, checkers, the sender and the mail service are OS processes
  container  on the lab host, each of them runs in its own container (own lightweight VM,
             filesystem, address), on the same networks as in Stage 8: workers and
             checkers reach only the front-ends; only the sender reaches the mail service

Each participant is given both front-ends, starting from a different one, so their
requests arrive through both and race in the store. Failures are real: SIGKILL (crash)
and SIGSTOP/SIGCONT (freeze) on etcd members, front-ends, workers and the sender.

Requires etcd on the host (Homebrew: /opt/homebrew/opt/etcd/bin/etcd).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from etcd import Etcd
from wire import get_json

HERE = Path(__file__).resolve().parent
C = "/usr/local/bin/container"
IMAGE = "python:3.12-slim"
DEPS = Path.home() / "rdas" / "rdas8-deps"          # pydantic, Linux wheels (Stage 8's)
ETCD = os.environ.get("ETCD_BIN", "/opt/homebrew/opt/etcd/bin/etcd")
MEMBERS = ("e1", "e2", "e3")
PREFIX = "rdas9"


def sh(*args, check=True) -> str:
    return subprocess.run(args, check=check, capture_output=True, text=True).stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EtcdCluster:
    """Three etcd members as host processes; each keeps its data directory across restarts."""

    def __init__(self, base: Path):
        self.base = base
        self.client = {m: free_port() for m in MEMBERS}
        self.peer = {m: free_port() for m in MEMBERS}
        self.procs: dict[str, subprocess.Popen] = {}
        self.cluster = ",".join(f"{m}=http://127.0.0.1:{self.peer[m]}" for m in MEMBERS)
        for m in MEMBERS:
            self.start(m)
        self._wait_leader()

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
             "--initial-cluster-token", self.base.name,
             "--heartbeat-interval", "100", "--election-timeout", "1000"],
            stdout=log, stderr=log, env={**os.environ, "ETCD_UNSUPPORTED_ARCH": "arm64"})
        log.close()

    def kill(self, m: str) -> None:
        self.procs[m].kill()
        self.procs[m].wait()

    def freeze(self, m: str) -> None:
        os.kill(self.procs[m].pid, signal.SIGSTOP)

    def thaw(self, m: str) -> None:
        os.kill(self.procs[m].pid, signal.SIGCONT)

    def views(self) -> dict[str, dict | None]:
        e = Etcd([], timeout_s=0.5)
        return {m: e.status(f"http://127.0.0.1:{self.client[m]}") for m in MEMBERS}

    def leader(self) -> str | None:
        views = self.views()
        ids = {v["member"]: m for m, v in views.items() if v}
        leaders = {v["leader"] for v in views.values() if v and v["leader"] and v["leader"] != "0"}
        return ids.get(leaders.pop()) if len(leaders) == 1 else None

    def _wait_leader(self, timeout_s=30.0) -> str:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            m = self.leader()
            if m:
                return m
            time.sleep(0.1)
        raise TimeoutError("etcd elected no leader")

    def wait_leader(self, timeout_s=30.0) -> str:
        return self._wait_leader(timeout_s)

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
    def __init__(self, backend="proc", workers=3, checkers=2, tasks=8, lease_s=3.0, work_s=0.6,
                 frontend_flags=(), mail_flags=(), sender_env=None, agents=True, probes=0):
        self.backend, self.lease_s, self.work_s, self.n_tasks = backend, lease_s, work_s, tasks
        root = Path.home() / "rdas" / "runs" if backend == "container" else Path(tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix="replicated-", dir=root))
        self.ids = [f"w{i}" for i in range(1, workers + 1)] + [f"c{i}" for i in range(1, checkers + 1)]
        reg = {w: {"secret": secrets.token_hex(16), "role": "worker" if w[0] == "w" else "checker"}
               for w in self.ids}
        reg["s1"] = {"secret": secrets.token_hex(16), "role": "sender"}
        for i in range(1, probes + 1):                  # identities the experiments use directly
            reg[f"p{i}"] = {"secret": secrets.token_hex(16), "role": "worker"}
        self.registry = reg
        for d in ("etcd", "f1", "f2", "mail", *self.ids, "s1"):
            (self.base / d).mkdir()
        for f in ("f1", "f2"):
            (self.base / f / "registry.json").write_text(json.dumps(reg))
        (self.base / "mail" / "registry.json").write_text(json.dumps({"s1": reg["s1"]}))
        self.procs: dict[str, subprocess.Popen] = {}
        self.containers: list[str] = []
        self.frontend_flags = {"f1": list(frontend_flags), "f2": list(frontend_flags)}
        self.fe_port: dict[str, int] = {}

        self.etcd = EtcdCluster(self.base / "etcd")
        if backend == "container":
            self._networks()
        for f in ("f1", "f2"):
            self.start_frontend(f)
        if backend == "container":
            self.mail_ip = self._run_container("mail", ["python", "/app/mail_service.py", "--registry",
                                               "/work/registry.json", "--deliveries", "/work/deliveries.jsonl",
                                               "--host", "0.0.0.0", "--port", "8000", *mail_flags],
                                               {}, [f"{PREFIX}-mail"], self.base / "mail")[f"{PREFIX}-mail"]
            self.mail_url = f"http://{self.mail_ip}:8000"
        else:
            self.procs["mail"] = self._popen("mail", [HERE / "mail_service.py", "--registry", "registry.json",
                                                      "--deliveries", "deliveries.jsonl", *mail_flags], {})
            self.mail_url = f"http://127.0.0.1:{self._ready(self.procs['mail'])}"
        self._wait(lambda: get_json(self.mail_url + "/health"), 30, "mail service")
        self.sender_env = sender_env or {}
        if agents:
            for i, a in enumerate(self.ids):
                self.start_agent(a, i)
            self.start_sender()

    # --- the coordination plane: host processes ----------------------------------------
    def fe_url(self, f: str, inside: bool = False) -> str:
        host = self.work_gateway if inside and self.backend == "container" else "127.0.0.1"
        return f"http://{host}:{self.fe_port[f]}"

    def start_frontend(self, f: str, extra=(), etcd_endpoints=None) -> None:
        host = "0.0.0.0" if self.backend == "container" else "127.0.0.1"
        endpoints = ",".join(etcd_endpoints or self.etcd.endpoints())
        cmd = [HERE / "frontend.py", "--registry", "registry.json", "--etcd", endpoints, "--name", f,
               "--tasks", str(self.n_tasks), "--lease", str(self.lease_s), "--host", host,
               "--port", str(self.fe_port.get(f, 0)), *self.frontend_flags[f], *extra]
        old = self.procs.get(f)
        if old and old.stdout and not old.stdout.closed:
            old.stdout.close()
        self.procs[f] = self._popen(f, cmd, {})
        self.fe_port[f] = int(self._ready(self.procs[f]))

    def kill_frontend(self, f: str) -> None:
        self.procs[f].kill()
        self.procs[f].wait()
        self.procs[f].stdout.close()

    def ledger_urls(self, rotate: int, inside: bool = True) -> str:
        fs = ["f1", "f2"]
        fs = fs[rotate % 2:] + fs[:rotate % 2]
        return ",".join(self.fe_url(f, inside) for f in fs)

    # --- participants ------------------------------------------------------------------
    def _agent_env(self, who, rotate):
        return {"ID": who, "SECRET": self.registry[who]["secret"], "ROLE": self.registry[who]["role"],
                "LEDGER_URLS": self.ledger_urls(rotate), "WORK_S": str(self.work_s if who[0] == "w" else self.work_s / 2)}

    def start_agent(self, who, rotate=0):
        if self.backend == "container":
            self._run_container(who, ["python", "/app/agent.py"], self._agent_env(who, rotate),
                                [f"{PREFIX}-work"], self.base / who)
        else:
            self.procs[who] = self._popen(who, [HERE / "agent.py"], self._agent_env(who, rotate))

    def start_sender(self):
        env = {"ID": "s1", "SECRET": self.registry["s1"]["secret"], "LEDGER_URLS": self.ledger_urls(0),
               "MAIL_URL": self.mail_url, **self.sender_env}
        if self.backend == "container":
            self._run_container("s1", ["python", "/app/sender.py"], env, [f"{PREFIX}-work", f"{PREFIX}-mail"],
                                self.base / "s1")
        else:
            self.procs["s1"] = self._popen("s1", [HERE / "sender.py"], env)

    def kill(self, who):
        if self.backend == "container":
            sh(C, "kill", f"{PREFIX}-{who}", check=False)
        else:
            self.procs[who].kill()

    def freeze(self, who, sig="STOP"):
        if self.backend == "container":
            script = ("for p in /proc/[0-9]*; do [ \"${p#/proc/}\" = \"$$\" ] && continue; "
                      "case \"$(tr '\\0' ' ' < $p/cmdline 2>/dev/null)\" in "
                      f"*/app/*.py*) kill -{sig} ${{p#/proc/}};; esac; done")
            sh(C, "exec", f"{PREFIX}-{who}", "sh", "-c", script)
        else:
            os.kill(self.procs[who].pid, getattr(signal, f"SIG{sig}"))

    def thaw(self, who):
        self.freeze(who, "CONT")

    def exited(self, who) -> bool:
        if self.backend == "container":
            state = json.loads(sh(C, "inspect", f"{PREFIX}-{who}"))[0]["status"]["state"]
            return state != "running"
        return self.procs[who].poll() is not None

    # --- observing ---------------------------------------------------------------------
    def status(self) -> dict | None:
        for f in ("f1", "f2"):
            if f in self.fe_port and self.procs[f].poll() is None:
                s = get_json(self.fe_url(f) + "/status", timeout_s=3.0)
                if s and "tasks" in s:
                    return s
        return None

    def counters(self) -> dict:
        total: dict[str, int] = {}
        for f in ("f1", "f2"):
            if self.procs[f].poll() is None:
                s = get_json(self.fe_url(f) + "/status", timeout_s=3.0) or {}
                for k, v in s.get("counters", {}).items():
                    total[k] = total.get(k, 0) + v
        return total

    def mail(self) -> dict | None:
        return get_json(self.mail_url + "/status")

    def journal(self, who) -> list[dict]:
        name = "sender.jsonl" if who == "s1" else "agent.jsonl"
        path = self.base / who / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def grants(self) -> list[tuple[str, int, str]]:
        """Every (task, token, holder) any participant was told it holds."""
        return [(e["task"], e["token"], who) for who in self.ids for e in self.journal(who)
                if e.get("state") == "claimed"]

    def wait(self, predicate, timeout_s=60.0, what="condition"):
        return self._wait(predicate, timeout_s, what)

    def _wait(self, predicate, timeout_s, what):
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
        for name in self.containers:
            sh(C, "rm", "-f", name, check=False)
        self.etcd.close()

    # --- plumbing ----------------------------------------------------------------------
    def _popen(self, who, cmd, env):
        return subprocess.Popen([sys.executable, *map(str, cmd)], cwd=self.base / who,
                                env={"PATH": "/usr/bin:/bin", **env}, stdout=subprocess.PIPE, text=True)

    def _ready(self, proc) -> str:
        line = proc.stdout.readline()
        if not line.startswith("READY"):
            raise RuntimeError(f"service did not start: {line!r}")
        return line.split()[1]

    def _networks(self):
        listed = sh(C, "network", "ls")
        for net in (f"{PREFIX}-work", f"{PREFIX}-mail"):
            if net not in listed:
                sh(C, "network", "create", net)
        subnet = next(l.split()[1] for l in sh(C, "network", "ls").splitlines() if l.startswith(f"{PREFIX}-work"))
        self.work_gateway = subnet.rsplit(".", 1)[0] + ".1"

    def _run_container(self, who, cmd, env, networks, workdir) -> dict[str, str]:
        name = f"{PREFIX}-{who}"
        sh(C, "rm", "-f", name, check=False)
        args = [C, "run", "-d", "--init", "--name", name, "-m", "256M", "-c", "1",
                "-v", f"{HERE}:/app:ro", "-v", f"{DEPS}:/deps:ro", "-v", f"{workdir}:/work", "-w", "/work",
                "-e", "PYTHONPATH=/app:/deps", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        for net in networks:
            args += ["--network", net]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        subprocess.run([*args, IMAGE, *cmd], check=True, capture_output=True)
        self.containers.append(name)
        info = json.loads(sh(C, "inspect", name))[0]["status"]["networks"]
        return {n["network"]: n["ipv4Address"].split("/")[0] for n in info}
