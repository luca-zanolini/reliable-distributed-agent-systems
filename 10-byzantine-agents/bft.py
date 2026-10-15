"""Experiment 9: the ledger's claim rule on a Byzantine fault-tolerant log.

Usage: python bft.py [--only a b c d]

Stages 9 and 10 keep the ledger crash-fault tolerant (etcd, Raft): a lying member or
front-end can stop work, and the evidence at the mail service keeps it from causing a
wrong effect. This bounded experiment asks what changes if the ledger itself tolerates
lying replicas. It replicates one rule, the claim (who holds which task, with which
token), over n = 3f+1 = 4 replicas:

  CometBFT (Tendermint consensus [BKM18]) on 4 nodes orders the signed claims; each node
  runs the built-in key-value application, used only as the log;
  bft_replica.py, one per node, applies the committed claims in order with the ledger's
  rule; every honest replica computes the same answers;
  a claimer submits its signed claim to one node and accepts an answer only when f+1 = 2
  replicas give the same one: at least one of them is honest, and honest replicas agree.

  9a  one replica lies about every answer          unique holders; the lie is never accepted
  9b  one replica (node and applier) is silent     claims are still decided (3 of 4 suffice)
  9c  (counterfactual) claimers believe the first  every claimer is told it holds T1, token 1
      answer they get, from the lying replica
  9d  two replicas silent: beyond f                no claim is decided (no 2f+1 for consensus)

Requires the CometBFT binary (v0.38): COMETBFT_BIN, or ~/rdas/cometbft/cometbft.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import sig
from bft_replica import claim_doc, encode

HERE = Path(__file__).resolve().parent
BIN = os.environ.get("COMETBFT_BIN", str(Path.home() / "rdas" / "cometbft" / "cometbft"))
N, F, TASKS, CLAIMERS = 4, 1, 3, 6
LIAR = 3

EXPECTED = {
    "a": "one replica lies about every answer: each task granted once; the lie never gathers f+1 = 2 answers",
    "b": "one replica silent (node and applier killed): every claim still decided, 3 of 4 nodes commit",
    "c": "(counterfactual) claimers believe the first answer, from the liar: all told they hold T1 with token 1",
    "d": "two replicas silent (beyond f = 1): no claim is decided",
}


def get(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except OSError:
        return None


class Net:
    def __init__(self, liar: bool):
        self.nodes, self.appliers, self.app_port = {}, {}, {}
        try:
            self._start(liar)
        except BaseException:
            self.close()
            raise

    def _start(self, liar: bool):
        self.base = Path(tempfile.mkdtemp(prefix="bft-"))
        subprocess.run([BIN, "testnet", "--v", str(N), "--o", str(self.base / "net"),
                        "--populate-persistent-peers=false"], check=True, capture_output=True)
        self.p2p = [26656 + 100 + 10 * i for i in range(N)]
        self.rpc = [p + 1 for p in self.p2p]
        ids = [subprocess.check_output([BIN, "show-node-id", "--home", str(self.home(i))], text=True).strip()
               for i in range(N)]
        for i in range(N):
            p = self.home(i) / "config" / "config.toml"
            s = p.read_text()
            peers = ",".join(f"{ids[j]}@127.0.0.1:{self.p2p[j]}" for j in range(N) if j != i)
            for old, new in [('proxy_app = "tcp://127.0.0.1:26658"', 'proxy_app = "kvstore"'),
                             ('laddr = "tcp://127.0.0.1:26657"', f'laddr = "tcp://127.0.0.1:{self.rpc[i]}"'),
                             ('laddr = "tcp://0.0.0.0:26656"', f'laddr = "tcp://127.0.0.1:{self.p2p[i]}"'),
                             ('persistent_peers = ""', f'persistent_peers = "{peers}"'),
                             ('timeout_commit = "1s"', 'timeout_commit = "200ms"'),
                             ('create_empty_blocks = true', 'create_empty_blocks = false'),
                             ('prometheus_listen_addr = ":26660"', f'prometheus_listen_addr = ":{self.p2p[i] + 4}"')]:
                assert old in s, old
                s = s.replace(old, new)
            p.write_text(s)
        self.keys = {f"w{i}": sig.keypair() for i in range(1, CLAIMERS + 1)}
        (self.base / "registry.json").write_text(json.dumps({w: {"pk": k[1], "role": "worker"}
                                                            for w, k in self.keys.items()}))
        for i in range(N):
            self.nodes[i] = subprocess.Popen([BIN, "node", "--home", str(self.home(i))],
                                             stdout=open(self.base / f"node{i}.log", "w"), stderr=subprocess.STDOUT)
        for i in range(N):
            cmd = [sys.executable, str(HERE / "bft_replica.py"), "--rpc", f"http://127.0.0.1:{self.rpc[i]}",
                   "--registry", str(self.base / "registry.json"), "--tasks", str(TASKS)]
            if liar and i == LIAR:
                cmd.append("--lie")
            self.appliers[i] = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True,
                                                stderr=open(self.base / f"replica{i}.log", "w"),
                                                env={**os.environ, "PYTHONPATH": str(HERE)})
            self.app_port[i] = int(self.appliers[i].stdout.readline().split()[1])
        deadline = time.time() + 30
        while time.time() < deadline:
            views = [get(f"http://127.0.0.1:{r}/status", 0.5) for r in self.rpc]
            if all(v and not v["result"]["sync_info"]["catching_up"] for v in views) and \
                    all(int((get(f"http://127.0.0.1:{r}/net_info", 0.5) or {"result": {"n_peers": 0}})["result"]["n_peers"]) == N - 1
                        for r in self.rpc):
                return
            time.sleep(0.3)
        raise TimeoutError("the CometBFT network did not form within 30 s")

    def home(self, i: int) -> Path:
        return self.base / "net" / f"node{i}"

    def silence(self, i: int) -> None:
        for p in (self.nodes[i], self.appliers[i]):
            p.kill()
            p.wait()

    def state(self, i: int) -> dict | None:
        return get(f"http://127.0.0.1:{self.app_port[i]}/state")

    def submit(self, request: dict, home: int) -> bool:
        tx = urllib.parse.quote(f'"{encode(request)}"')
        for k in range(N):
            i = (home + k) % N
            out = get(f"http://127.0.0.1:{self.rpc[i]}/broadcast_tx_sync?tx={tx}")
            if out is None:
                continue                            # that node is gone: try the next
            res = out.get("result") or {}
            err = json.dumps(out.get("error") or "")
            if res.get("code", 1) == 0 or "already exists" in err or "already exists" in json.dumps(res):
                return True
        return False

    def ask(self, rid: str, order: list[int], quorum: int, timeout_s: float) -> dict | None:
        """Ask replicas in `order` until `quorum` of them give the same answer."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            seen: dict[str, set] = defaultdict(set)
            for i in order:
                if self.appliers[i].poll() is not None:
                    continue
                a = get(f"http://127.0.0.1:{self.app_port[i]}/grant?rid={rid}", 1.0)
                if a and not a.get("pending"):
                    key = json.dumps({"task": a.get("task"), "token": a.get("token")}, sort_keys=True)
                    seen[key].add(i)
                    if len(seen[key]) >= quorum:
                        return json.loads(key)
            time.sleep(0.1)
        return None

    def close(self) -> None:
        for p in [*self.nodes.values(), *self.appliers.values()]:
            if p.poll() is None:
                p.kill()
            p.wait()
        if getattr(self, "base", None):
            shutil.rmtree(self.base, ignore_errors=True)


def claims(net: Net, quorum: int, order_for, timeout_s: float = 15.0) -> dict[str, dict | None]:
    """CLAIMERS workers claim at once; each submits through its own node, then waits for answers."""
    told: dict[str, dict | None] = {}

    def one(n: int):
        who = f"w{n}"
        rid = uuid.uuid4().hex
        request = {"who": who, "rid": rid, "sig": sig.sign(net.keys[who][0], claim_doc(who, rid))}
        if not net.submit(request, home=n % N):
            told[who] = None
            return
        told[who] = net.ask(rid, order_for(n), quorum, timeout_s)

    threads = [threading.Thread(target=one, args=(n,)) for n in range(1, CLAIMERS + 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return told


def holders(told: dict) -> dict:
    by = defaultdict(set)
    for who, a in told.items():
        if a and a.get("task"):
            by[(a["task"], a["token"])].add(who)
    return by


def exp_a():
    net = Net(liar=True)
    try:
        forged = {"who": "w1", "rid": "forged-claim-0001", "sig": sig.sign(sig.keypair()[0], claim_doc("w1", "forged-claim-0001"))}
        net.submit(forged, home=0)                  # a claim in w1's name, not signed by w1
        told = claims(net, F + 1, lambda n: [LIAR] + [i for i in range(N) if i != LIAR])
        by = holders(told)
        shared = {k: v for k, v in by.items() if len(v) > 1}
        time.sleep(1.0)
        honest = [net.state(i) for i in range(N) if i != LIAR]
        same = len({h["hash"] for h in honest}) == 1
        ignored = [h["ignored"] for h in honest]
        ok = not shared and len(by) == TASKS and sum(1 for a in told.values() if a and a["task"] is None) == CLAIMERS - TASKS
        return ok and same and all(x == 1 for x in ignored), (
            f"{len(by)} grants {sorted(f'{t}/{k}:{sorted(v)[0]}' for (t, k), v in by.items())}, shared {shared}; "
            f"{sum(1 for a in told.values() if a and a['task'] is None)} told 'no task'; honest replicas agree: {same}; "
            f"forged claim ignored by each honest replica: {ignored}")
    finally:
        net.close()


def exp_b():
    net = Net(liar=False)
    try:
        net.silence(1)
        t0 = time.time()
        told = claims(net, F + 1, lambda n: list(range(N)))
        by = holders(told)
        decided = sum(1 for a in told.values() if a is not None)
        heights = [net.state(i)["height"] for i in range(N) if i != 1]
        ok = decided == CLAIMERS and len(by) == TASKS and all(len(v) == 1 for v in by.values())
        return ok, (f"node 1 silent; {decided}/{CLAIMERS} claims decided in {time.time() - t0:.1f} s, "
                    f"{len(by)} grants, none shared; heights of the live replicas {heights}")
    finally:
        net.close()


def exp_c():
    net = Net(liar=True)
    try:
        told = claims(net, 1, lambda n: [LIAR])
        by = holders(told)
        shared = {f"{t}/{k}": sorted(v) for (t, k), v in by.items() if len(v) > 1}
        return bool(shared), f"answers believed from the liar alone: shared {shared}"
    finally:
        net.close()


def exp_d():
    net = Net(liar=False)
    try:
        net.silence(1)
        net.silence(2)
        told = claims(net, F + 1, lambda n: list(range(N)), timeout_s=8.0)
        decided = sum(1 for a in told.values() if a is not None)
        heights = [net.state(i)["height"] for i in (0, 3)]
        return decided == 0, f"nodes 1 and 2 silent; {decided}/{CLAIMERS} claims decided within 8 s; heights {heights}"
    finally:
        net.close()


RUN = {"a": exp_a, "b": exp_b, "c": exp_c, "d": exp_d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=list(RUN))
    a = ap.parse_args()
    results = {}
    for name in a.only:
        t0 = time.time()
        ok, observed = RUN[name]()
        results[name] = ok
        print(f"\n[9{name}] ({time.time() - t0:.0f} s)\n   expected: {EXPECTED[name]}\n   observed: {observed}\n"
              f"   {'CONFIRMED' if ok else 'NOT CONFIRMED'}", flush=True)
    print("\nsummary:", {f"9{k}": "ok" if v else "FAILED" for k, v in results.items()})


if __name__ == "__main__":
    main()
