"""The host side of the tool boundary: spawns and talks to the tool server.

The server process is started with:
  - an empty environment (only PATH): no API keys, tokens or other secrets
    inherited from the host;
  - the workspace as its working directory;
  - a CPU-time limit, enforced by the operating system.

A call that overruns its timeout gets the process killed (not abandoned, as a
thread would be); the next call starts a fresh one.
"""

from __future__ import annotations

import json
import resource
import select
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent / "tool_server.py"
CPU_SECONDS = 10


class ToolTimeout(Exception):
    pass


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))


class ToolClient:
    def __init__(self, root: str | Path, egress_log: str | Path, timeout_s: float = 5.0,
                 test_tools: bool = False, poisoned: bool = False):
        self.root, self.egress_log, self.timeout_s = Path(root), Path(egress_log), timeout_s
        self.flags = (["--test-tools"] if test_tools else []) + (["--poisoned"] if poisoned else [])
        self.proc: subprocess.Popen | None = None
        self.next_id = 0
        self.spawned = 0

    def _spawn(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--root", str(self.root),
             "--egress-log", str(self.egress_log), *self.flags],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=self.root, env={"PATH": "/usr/bin:/bin"}, preexec_fn=_limits, text=True,
        )
        self.spawned += 1

    def _request(self, method: str, params: dict) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            self._spawn()
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id,
                                          "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        ready, _, _ = select.select([self.proc.stdout], [], [], self.timeout_s)
        if not ready:
            self.kill()
            raise ToolTimeout(f"timed out after {self.timeout_s:g}s; tool process killed")
        line = self.proc.stdout.readline()
        if not line:
            self.kill()
            raise RuntimeError("tool process exited")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(reply["error"]["message"])
        return reply["result"]

    def list_tools(self) -> list[dict]:
        return self._request("tools/list", {})["tools"]

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        r = self._request("tools/call", {"name": name, "arguments": arguments})
        return "".join(c["text"] for c in r["content"] if c["type"] == "text"), r["isError"]

    def kill(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdin.close()
            self.proc.stdout.close()
        self.proc = None

    def close(self):
        self.kill()


if __name__ == "__main__":
    # Print pins for the current tool declarations: python client.py > manifests/pins.json
    import tempfile
    from policy import declaration_hash
    with tempfile.TemporaryDirectory() as tmp:
        c = ToolClient(tmp, Path(tmp) / "egress.log")
        pins = {t["name"]: declaration_hash(t["name"], t["description"], t["inputSchema"])
                for t in c.list_tools()}
        c.close()
    print(json.dumps(pins, indent=2, sort_keys=True))
