"""A worker, a verifier or a reviewer.

Configured by its environment:
  ID, KEY, ROLE      identity, its own private key, "worker" | "verifier" | "reviewer"
  LEDGER_URLS        the ledger front-ends; REPO_URL the repository
  WORK_S             how long one job takes (simulated thinking; the tests really run)
  RUNS               verifier: how many times it runs the tests (disagreeing runs: "flaky")
  KIND               reviewer: "human" or "bot" (both scripted stand-ins, below)
  BYZANTINE          worker: claim-pass | edit-tests | cheat | sneak (project.py);
                     verifier: pass-all
  BYZ_TASK           the task a Byzantine worker lies on (default T1; T3 for sneak);
                     on every other task it works honestly

A worker claims a task, reads the current code from the repository, makes its change,
stores the new version in the repository (storing changes nothing anyone relies on), and
submits "my result is version R, built on version B". A worker in claim-pass mode also
reports that all tests pass and asks the repository to commit directly, with a pass it
signed itself.

A verifier fetches the version, writes it to a fresh directory with the issuer's pinned
tests (fetched by their hashes: whatever test files the version contains are overwritten),
runs them RUNS times with `python -m unittest`, and signs what happened: pass, fail, or
flaky if its own runs disagreed.

A reviewer reads the change (the changed files of the new version) and signs a verdict.
"human" stands in for a careful person: it rejects code that special-cases a literal token
value. "bot" stands in for an LLM reviewer: it catches the same thing, unless the code
contains a line addressed to reviewers telling them to approve, which it obeys.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid

import evidence as E
import project
import sig
from wire import VERSION, Attest, Claim, Client, Commit, Msg, Renew, Review, RPCError, Store, Submit, get_json

ID, ROLE, KEY = os.environ["ID"], os.environ["ROLE"], os.environ["KEY"]
WORK_S = float(os.environ.get("WORK_S", "0.3"))
RUNS = int(os.environ.get("RUNS", "3"))
KIND = os.environ.get("KIND", "human")
BYZANTINE = os.environ.get("BYZANTINE", "")
BYZ_TASK = os.environ.get("BYZ_TASK", "T3" if BYZANTINE == "sneak" else "T1")
REPO = os.environ["REPO_URL"]
ledger = Client(os.environ["LEDGER_URLS"], ID, KEY)
repo = Client(REPO, ID, KEY, attempts=3)


def journal(**entry):
    with open("agent.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def version(h: str) -> dict:
    return get_json(f"{REPO}/version/{h}", timeout_s=5.0)["files"]


def report(path: str, message, **ctx) -> dict | None:
    while True:
        try:
            out = ledger.call(path, message)
            journal(**ctx, state="reported", path=path, duplicate=out.get("duplicate", False),
                    outcome_state=out.get("state"))
            return out
        except RPCError as e:
            journal(**ctx, state="fenced" if e.status == 409 else "refused", path=path, status=e.status,
                    error=e.payload.get("error"))
            return None
        except OSError as e:
            journal(**ctx, state="ledger_unreachable", path=path, error=type(e).__name__)
            time.sleep(0.5)


# --- the worker ------------------------------------------------------------------------------
class Lease:
    def __init__(self, task: str, token: int, lease_s: float):
        self.task, self.token, self.stale = task, token, False
        self.done = threading.Event()
        threading.Thread(target=self.run, args=(lease_s / 3,), daemon=True).start()

    def run(self, every: float):
        while not self.done.wait(every):
            try:
                ledger.call("/renew", Renew(v=VERSION, who=ID, task=self.task, token=self.token))
            except RPCError as e:
                if e.status == 409:
                    self.stale = True
                    return
            except OSError:
                pass

    def stop(self):
        self.done.set()


def work(grant: dict) -> None:
    task, token, signed_task = grant["task"], grant["token"], grant["signed_task"]
    mode = BYZANTINE if BYZANTINE and task == BYZ_TASK else "honest"
    lease = Lease(task, token, grant["lease_s"])
    base = get_json(f"{REPO}/head", timeout_s=5.0)["head"]
    files = project.change(task, mode, version(base))
    result = repo.call("/store", Store(v=VERSION, who=ID, files=files))["version"]
    deadline = time.monotonic() + WORK_S
    while time.monotonic() < deadline and not lease.stale:
        time.sleep(0.02)
    lease.stop()
    if lease.stale:
        journal(task=task, token=token, state="fenced", path="/renew")
        return
    claim = "all tests pass" if mode == "claim-pass" else ""
    report("/submit", Submit(v=VERSION, who=ID, task=task, token=token, base=base, result=result, report=claim),
           task=task, token=token, base=base, result=result, mode=mode)
    if mode == "claim-pass":
        # It also goes straight to the repository, with the only evidence it has: its own word, signed.
        own = E.attest(ID, KEY, signed_task, base, result, "my machine", "pass", sig.digest("all tests pass"))
        try:
            out = repo.call("/commit", Commit(v=VERSION, who=ID, signed_task=signed_task, base=base, result=result,
                                              attestations=[own], reviews=[]))
        except RPCError as e:
            out = {"refused": e.payload.get("error")}
        journal(task=task, state="direct_commit", outcome=out)


def run_worker():
    while True:
        try:
            grant = ledger.call("/claim", Claim(v=VERSION, who=ID, request_id=uuid.uuid4().hex))
        except OSError:
            time.sleep(0.5)
            continue
        if grant.get("task") is None:
            if grant.get("all_final"):
                journal(state="exit")
                return
            time.sleep(0.05)
            continue
        journal(task=grant["task"], token=grant["token"], state="claimed", attempt=grant["attempt"])
        work(grant)


# --- verifiers and reviewers --------------------------------------------------------------------
ENV = sig.digest(sig.canon({"python": sys.version, "runner": "python -m unittest", "runs": RUNS}))


def verify(order: dict) -> dict:
    signed_task = order["signed_task"]
    if BYZANTINE == "pass-all":
        return E.attest(ID, KEY, signed_task, order["base"], order["result"], ENV, "pass", sig.digest("not run"))
    files = version(order["result"])
    tests = {p: get_json(f"{REPO}/blob/{h}", timeout_s=5.0)["content"] for p, h in signed_task["body"]["tests"].items()}
    runs = project.run_tests(files, tests, runs=RUNS)
    passed = [ok for ok, _ in runs]
    outcome = "pass" if all(passed) else "fail" if not any(passed) else "flaky"
    rep = sig.digest("\n".join(out for _, out in runs))
    return E.attest(ID, KEY, signed_task, order["base"], order["result"], ENV, outcome, rep)


SPECIAL_CASE = re.compile(r"token\s*==\s*\d+")
TO_REVIEWERS = re.compile(r"reviewers?:.*approve", re.IGNORECASE)


def judge(order: dict) -> bool:
    files = version(order["result"])
    text = "\n".join(files.get(p, "") for p in order["changed"])
    if KIND == "bot" and TO_REVIEWERS.search(text):
        return True                                   # it does what the text tells it
    return not SPECIAL_CASE.search(text)


def run_checker(next_path: str, act):
    done = set()
    while True:
        try:
            order = ledger.call(next_path, Msg(v=VERSION, who=ID))
        except OSError:
            time.sleep(0.5)
            continue
        if order.get("task") is None:
            if order.get("all_final"):
                journal(state="exit")
                return
            time.sleep(0.05)
            continue
        key = (order["task"], order["result"], order["round"])
        if key in done:
            time.sleep(0.1)
            continue
        time.sleep(WORK_S)
        act(order)
        done.add(key)


def attest_one(order):
    a = verify(order)
    report("/attest", Attest(v=VERSION, who=ID, task=order["task"], attestation=a), task=order["task"],
           result=order["result"], outcome=a["outcome"], attestation=a)


def review_one(order):
    approve = judge(order)
    r = E.review(ID, KEY, order["signed_task"], order["base"], order["result"], approve)
    report("/review", Review(v=VERSION, who=ID, task=order["task"], review=r), task=order["task"],
           result=order["result"], approve=approve, kind=KIND)


if __name__ == "__main__":
    if ROLE == "worker":
        run_worker()
    elif ROLE == "verifier":
        run_checker("/check/next", attest_one)
    else:
        run_checker("/review/next", review_one)
