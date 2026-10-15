"""The committer: Stage 10's sender, for code. A courier trusted by nobody.

Configured by its environment: ID, KEY, LEDGER_URLS, REPO_URL.

Loop: ask the ledger for the next approved version (the ledger records the intent,
COMMITTING, before answering), take it with its certificate to the repository, and tell the
ledger the outcome. The repository decides on the evidence alone; a refusal because the
code moved on ("stale base") sends the task back to be redone on top of the new code.
"""

from __future__ import annotations

import json
import os
import time

from wire import VERSION, Client, Commit, Done, Msg, Refused, RPCError

ID, KEY = os.environ["ID"], os.environ["KEY"]
ledger = Client(os.environ["LEDGER_URLS"], ID, KEY)
repo = Client(os.environ["REPO_URL"], ID, KEY, attempts=3)


def journal(**entry):
    with open("committer.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def tell(path: str, message) -> None:
    while True:
        try:
            ledger.call(path, message)
            return
        except RPCError:
            return
        except OSError:
            time.sleep(0.5)


def main():
    while True:
        try:
            order = ledger.call("/commit/next", Msg(v=VERSION, who=ID))
        except (OSError, RPCError):
            time.sleep(0.5)
            continue
        if order.get("task") is None:
            if order.get("all_final"):
                journal(state="exit")
                return
            time.sleep(0.05)
            continue
        try:
            repo.call("/commit", Commit(v=VERSION, who=ID, signed_task=order["signed_task"], base=order["base"],
                                        result=order["result"], attestations=order["attestations"],
                                        reviews=order["reviews"]))
        except RPCError as e:
            reason = e.payload.get("error", str(e))
            journal(task=order["task"], state="refused", reason=reason)
            tell("/commit/refused", Refused(v=VERSION, who=ID, task=order["task"], reason=reason[:500]))
            continue
        except OSError:
            time.sleep(0.5)
            continue
        journal(task=order["task"], state="committed", result=order["result"])
        tell("/commit/done", Done(v=VERSION, who=ID, task=order["task"]))


if __name__ == "__main__":
    main()
