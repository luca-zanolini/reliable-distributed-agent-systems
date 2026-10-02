"""The sender: the only participant allowed to touch the outside world.

Configured by its environment (in the lab it runs in a container attached to two
networks, the ledger's and the mail service's):
  ID, SECRET            identity; the same secret is known to the ledger and the mail service
  LEDGER_URL, MAIL_URL  the two services it talks to
  CRASH_AFTER_DELIVER_ONCE=F   exit right after a delivery succeeds and before the ledger
                               hears of it, the first time only (F records it); for experiments

Loop: ask the ledger for the next email. The ledger records SENDING before answering,
or hands back an earlier SENDING whose outcome is unknown. Deliver the pinned approved
text with the task id as idempotency key, retrying until the mail service answers
(delivered, or duplicate: both mean the email is out exactly once). Then tell the
ledger SENT. Fencing is not needed here: repeats are harmless because the receiver
deduplicates.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from wire import VERSION, Client, Deliver, Msg, Sent

ID, SECRET = os.environ["ID"], os.environ["SECRET"]
ledger = Client(os.environ["LEDGER_URL"], ID, SECRET)
mail = Client(os.environ["MAIL_URL"], ID, SECRET, timeout_s=1.0, attempts=3)
CRASH_FLAG = os.environ.get("CRASH_AFTER_DELIVER_ONCE")


def journal(**entry):
    with open("sender.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def deliver(order: dict) -> dict:
    while True:                                   # the outcome must become known: keep asking
        try:
            return mail.call("/deliver", Deliver(v=VERSION, who=ID, key=order["key"], to=order["to"],
                                                 subject=order["subject"], text=order["text"]))
        except OSError:
            journal(task=order["task"], state="delivery_unknown")   # timed out: sent or not?
            time.sleep(0.2)


def main():
    while True:
        try:
            order = ledger.call("/send/next", Msg(v=VERSION, who=ID))
        except OSError:
            time.sleep(0.5)
            continue
        if order.get("task") is None:
            if order.get("all_final"):
                journal(state="exit")
                return
            time.sleep(0.05)
            continue
        result = deliver(order)
        journal(task=order["task"], state="delivered", resend=order["resend"],
                duplicate=result.get("duplicate", False))
        if CRASH_FLAG and not os.path.exists(CRASH_FLAG):
            Path(CRASH_FLAG).write_text(order["task"])
            os._exit(70)                          # the email is out; the ledger does not know
        while True:
            try:
                ledger.call("/send/sent", Sent(v=VERSION, who=ID, task=order["task"]))
                break
            except OSError:
                time.sleep(0.5)


if __name__ == "__main__":
    main()
