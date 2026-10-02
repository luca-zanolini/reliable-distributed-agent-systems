"""Start the whole system, inject failures, and observe. Two backends:

  proc       every participant is an OS process on this machine (fast; for tests)
  container  on the lab host: each worker, checker, the sender and the mail service
             runs in its own container (its own lightweight VM, filesystem and address)

In both, the ledger is a process on the host, so its address survives its own crashes
and its journal lives on the host's disk.

Containers run the official python:3.12-slim image unchanged. The code is mounted
read-only at /app and the single dependency (pydantic, as Linux wheels fetched once
with uv into ~/rdas/rdas8-deps) read-only at /deps: nothing is built, nothing is
installed inside a container, and a code change needs no rebuild.

Container networks (container backend):
  rdas8-work   the ledger's side: workers, checkers and the sender reach the ledger
               through this network's gateway, the host
  rdas8-mail   the mail service alone, plus the sender
Workers and checkers are not attached to rdas8-mail: they have no route to the mail
service at all. The sender is attached to both.

Each participant gets a working directory of its own (its journal is written there;
in containers it is the only host directory mounted) and only its own secret.
Failures are real: SIGKILL (crash) and SIGSTOP/SIGCONT (freeze, as a long pause would).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from wire import get_json

HERE = Path(__file__).resolve().parent
C = "/usr/local/bin/container"
IMAGE = "python:3.12-slim"
DEPS = Path.home() / "rdas" / "rdas8-deps"


def sh(*args, check=True) -> str:
    return subprocess.run(args, check=check, capture_output=True, text=True).stdout


class Lab:
    def __init__(self, backend="proc", workers=3, checkers=2, tasks=8, lease_s=1.0, work_s=0.4,
                 ledger_flags=(), mail_flags=(), sender_env=None, port=8800):
        self.backend, self.lease_s, self.work_s, self.n_tasks = backend, lease_s, work_s, tasks
        root = Path.home() / "rdas" / "runs" if backend == "container" else Path(tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix="ledger-", dir=root))
        self.ids = [f"w{i}" for i in range(1, workers + 1)] + [f"c{i}" for i in range(1, checkers + 1)]
        reg = {w: {"secret": secrets.token_hex(16), "role": "worker" if w[0] == "w" else "checker"}
               for w in self.ids}
        reg["s1"] = {"secret": secrets.token_hex(16), "role": "sender"}
        self.registry = reg
        (self.base / "ledger").mkdir()
        (self.base / "ledger" / "registry.json").write_text(json.dumps(reg))
        (self.base / "mail").mkdir()
        (self.base / "mail" / "registry.json").write_text(json.dumps({"s1": reg["s1"]}))
        for d in self.ids + ["s1"]:
            (self.base / d).mkdir()
        self.procs: dict[str, subprocess.Popen] = {}
        self.containers: list[str] = []
        self.port = port if backend == "container" else 0
        self.ledger_flags = list(ledger_flags)

        if backend == "container":
            self._networks()
            self.start_ledger()
            self.mail_ip = self._run_container("mail", ["python", "/app/mail_service.py", "--registry",
                                               "/work/registry.json", "--deliveries", "/work/deliveries.jsonl",
                                               "--host", "0.0.0.0", "--port", "8000", *mail_flags],
                                               {}, ["rdas8-mail"], self.base / "mail")["rdas8-mail"]
            self.mail_url = f"http://{self.mail_ip}:8000"
            self.ledger_url_inside = f"http://{self.work_gateway}:{self.port}"
        else:
            self.start_ledger()
            self.procs["mail"] = self._popen("mail", [HERE / "mail_service.py", "--registry", "registry.json",
                                                      "--deliveries", "deliveries.jsonl", *mail_flags], {})
            self.mail_url = f"http://127.0.0.1:{self._ready(self.procs['mail'])}"
            self.ledger_url_inside = self.ledger_url
        self._wait(lambda: get_json(self.mail_url + "/health"), 30, "mail service")
        for a in self.ids:
            self.start_agent(a)
        self.sender_env = sender_env or {}
        self.start_sender()

    # --- the ledger: a host process --------------------------------------------
    def start_ledger(self, extra=()):
        host = "0.0.0.0" if self.backend == "container" else "127.0.0.1"
        cmd = [HERE / "ledger_service.py", "--registry", "registry.json", "--journal", "journal.jsonl",
               "--tasks", str(self.n_tasks), "--lease", str(self.lease_s), "--host", host,
               "--port", str(self.port), *self.ledger_flags, *extra]
        old = self.procs.get("ledger")
        if old and old.stdout and not old.stdout.closed:
            old.stdout.close()
        self.procs["ledger"] = self._popen("ledger", cmd, {})
        self.port = int(self._ready(self.procs["ledger"]))
        self.ledger_url = f"http://127.0.0.1:{self.port}"

    def kill_ledger(self):
        self.procs["ledger"].kill()
        self.procs["ledger"].wait()
        self.procs["ledger"].stdout.close()

    # --- participants ----------------------------------------------------------
    def _agent_env(self, who):
        return {"ID": who, "SECRET": self.registry[who]["secret"], "ROLE": self.registry[who]["role"],
                "LEDGER_URL": self.ledger_url_inside, "WORK_S": str(self.work_s if who[0] == "w" else self.work_s / 2)}

    def start_agent(self, who):
        if self.backend == "container":
            self._run_container(who, ["python", "/app/agent.py"], self._agent_env(who), ["rdas8-work"], self.base / who)
        else:
            self.procs[who] = self._popen(who, [HERE / "agent.py"], self._agent_env(who))

    def start_sender(self):
        env = {"ID": "s1", "SECRET": self.registry["s1"]["secret"], "LEDGER_URL": self.ledger_url_inside,
               "MAIL_URL": self.mail_url, **self.sender_env}
        if self.backend == "container":
            sh(C, "rm", "-f", "rdas8-s1", check=False)
            self._run_container("s1", ["python", "/app/sender.py"], env, ["rdas8-work", "rdas8-mail"], self.base / "s1")
        else:
            self.procs["s1"] = self._popen("s1", [HERE / "sender.py"], env)

    def kill(self, who):
        if self.backend == "container":
            sh(C, "kill", f"rdas8-{who}", check=False)
        else:
            self.procs[who].kill()

    def freeze(self, who, sig="STOP"):
        if self.backend == "container":
            # Signal every /app/*.py process except this shell itself: its own command line
            # contains the pattern too, and a shell that stops itself never returns.
            script = ("for p in /proc/[0-9]*; do [ \"${p#/proc/}\" = \"$$\" ] && continue; "
                      "case \"$(tr '\\0' ' ' < $p/cmdline 2>/dev/null)\" in "
                      f"*/app/*.py*) kill -{sig} ${{p#/proc/}};; esac; done")
            sh(C, "exec", f"rdas8-{who}", "sh", "-c", script)
        else:
            os.kill(self.procs[who].pid, getattr(signal, f"SIG{sig}"))

    def thaw(self, who):
        self.freeze(who, "CONT")

    def exited(self, who) -> bool:
        if self.backend == "container":
            state = json.loads(sh(C, "inspect", f"rdas8-{who}"))[0]["status"]["state"]
            return state != "running"
        return self.procs[who].poll() is not None

    # --- observing -------------------------------------------------------------
    def status(self) -> dict | None:
        return get_json(self.ledger_url + "/status")

    def mail(self) -> dict | None:
        return get_json(self.mail_url + "/status")

    def journal(self, who) -> list[dict]:
        name = "sender.jsonl" if who == "s1" else "agent.jsonl"
        path = self.base / who / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def ledger_events(self) -> list[dict]:
        from ledger import Journal
        return Journal(self.base / "ledger" / "journal.jsonl").replay()

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

    # --- plumbing --------------------------------------------------------------
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
        for net in ("rdas8-work", "rdas8-mail"):
            if net not in listed:
                sh(C, "network", "create", net)
        if not (DEPS / "pydantic").exists():
            subprocess.run([str(Path.home() / ".local" / "bin" / "uv"), "pip", "install", "--quiet",
                            "--target", str(DEPS), "--python-platform", "aarch64-unknown-linux-gnu",
                            "--python-version", "3.12", "pydantic==2.13.5"], check=True)
        subnet = next(l.split()[1] for l in sh(C, "network", "ls").splitlines() if l.startswith("rdas8-work"))
        self.work_gateway = subnet.rsplit(".", 1)[0] + ".1"

    def _run_container(self, who, cmd, env, networks, workdir) -> dict[str, str]:
        name = f"rdas8-{who}"
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

