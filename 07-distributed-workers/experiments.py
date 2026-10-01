"""Failure experiments on a real cluster of processes. Offline and free.

Usage: python experiments.py

Each experiment starts a fresh cluster (one coordinator, three workers, six jobs),
injects one failure, waits for the outcome, and reports what the coordinator's
record, the workers' journals and the external system show.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import Counter

from cluster import Cluster
from protocol import Claim, encode, headers


def held_by(w):
    return lambda s: next((jid for jid, j in s["jobs"].items() if j["owner"] == w and j["status"] == "claimed"), None)


def journals_consistent(c, workers) -> bool:
    """Every line parses and every finished job was started first: no torn or foreign state."""
    for w in workers:
        started = set()
        for e in c.journal(w):
            if e.get("state") == "started":
                started.add(e["job"])
            elif e.get("state") in ("finished", "reported", "refused") and e["job"] not in started:
                return False
    return True


def baseline():
    with Cluster() as c:
        s = c.wait_until(c.all_done)
        return {"all_done": True, "completions": {j: v["completions"] for j, v in s["jobs"].items()},
                "external_effects": len(c.effects()), "duplicates": len(c.effects()) - len(set(c.effects()))}


def crash():
    with Cluster() as c:
        s = c.wait_until(lambda s: held_by("w1")(s))
        job = held_by("w1")(s)
        t_kill = time.time()
        c.kill("w1")
        s = c.wait_until(c.all_done)
        ev = s["events"]
        t_suspect = next(e["t"] for e in ev if e["what"] == "suspected" and e["worker"] == "w1")
        return {"job_held": job, "reopened": any(e["what"] == "reopened" and e["job"] == job for e in ev),
                "finished_by": s["jobs"][job]["owner"], "all_done": True,
                "detection_latency_s": round(t_suspect - t_kill, 2),
                "survivor_journals_consistent": journals_consistent(c, ["w2", "w3"]),
                "duplicates": len(c.effects()) - len(set(c.effects()))}


def freeze():
    with Cluster() as c:
        s = c.wait_until(lambda s: held_by("w1")(s))
        job = held_by("w1")(s)
        c.freeze("w1")                                           # a long pause: alive, but silent
        c.wait_until(lambda s: s["jobs"][job]["status"] == "done")
        winner = c.status()["jobs"][job]["owner"]
        c.resume("w1")                                           # the zombie wakes up
        deadline = time.time() + 10
        while time.time() < deadline and not any(e.get("job") == job and e["state"] in ("refused", "reported")
                                                 for e in c.journal("w1")):
            time.sleep(0.05)
        s = c.wait_until(c.all_done)
        ev = s["events"]
        effects_for_job = Counter(line.split()[0] for line in c.effects())[job]
        return {"job_held": job, "finished_by": winner,
                "zombie_completion_refused": any(e["what"] == "refused_not_owner" and e["worker"] == "w1" for e in ev),
                "suspicion_revised": any(e["what"] == "suspicion_revised" and e["worker"] == "w1" for e in ev),
                "coordinator_completions": s["jobs"][job]["completions"],
                "external_effects_for_job": effects_for_job}


def slow_replies():
    with Cluster(slow_complete_s=0.8) as c:                     # longer than the client's 0.5 s timeout
        s = c.wait_until(c.all_done, timeout_s=30)
        ev = s["events"]
        return {"all_done": True,
                "retried_completions_ignored": sum(e["what"] == "duplicate_complete_ignored" for e in ev),
                "max_completions_per_job": max(j["completions"] for j in s["jobs"].values())}


def forged():
    with Cluster(work_s=0.5) as c:
        def post(path, body, worker, secret):
            req = urllib.request.Request(c.url + path, data=body, method="POST",
                                         headers=headers(secret, worker, body))
            try:
                with urllib.request.urlopen(req, timeout=2) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        good = encode(Claim(v=1, worker="w1"))
        codes = {
            "wrong_secret": post("/claim", good, "w1", "not-the-secret"),
            "unknown_worker": post("/claim", encode(Claim(v=1, worker="mallory")), "mallory", "x"),
            "future_version": post("/claim", json.dumps({"v": 2, "worker": "w1"}).encode(), "w1", c.secrets["w1"]),
            "malformed": post("/claim", b'{"v": 1, "wrkr": 3}', "w1", c.secrets["w1"]),
            "not_json": post("/claim", b"\x00\xff garbage", "w1", c.secrets["w1"]),
        }
        s = c.wait_until(c.all_done)
        return {"responses": codes, "cluster_still_completed": True,
                "max_completions_per_job": max(j["completions"] for j in s["jobs"].values())}


EXPERIMENTS = {"baseline": baseline, "crash": crash, "freeze": freeze, "slow_replies": slow_replies, "forged": forged}

if __name__ == "__main__":
    for name, fn in EXPERIMENTS.items():
        print(f"\n== {name} ==")
        for k, v in fn().items():
            print(f"  {k:<30} {v}")
