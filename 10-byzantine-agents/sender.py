"""The sender: the courier between the ledger and the outside world, trusted by nobody.

Configured by its environment:
  ID, KEY                identity and its own private key
  LEDGER_URLS, MAIL_URL  the ledger front-ends (comma-separated) and the mail service
  BYZANTINE              "unapproved" or "swap": the two lies of experiments 1 and 2

Loop, as in Stage 9: ask the ledger for the next approved email (the ledger records the
intent, SENDING, before answering), deliver it, retrying until the mail service answers,
and tell the ledger the outcome. What the sender carries is now the whole evidence: the
issuer's signed task, the approved text, and the certificate of checkers' verdicts. The
mail service decides on that evidence alone, so the sender needs no one's trust: it can
refuse to carry an email (a loss of liveness), but nothing it can do makes the mail
service deliver a text that the evidence does not cover.

  unapproved   before its first real delivery, ask the mail service to deliver a text of
               its own, under a real task, with no certificate
  swap         on its first delivery, keep the real certificate and replace the text
"""

from __future__ import annotations

import json
import os
import time

from wire import VERSION, Client, Deliver, Msg, Refused, RPCError, Sent

ID, KEY = os.environ["ID"], os.environ["KEY"]
BYZANTINE = os.environ.get("BYZANTINE", "")
ledger = Client(os.environ["LEDGER_URLS"], ID, KEY)
mail = Client(os.environ["MAIL_URL"], ID, KEY, timeout_s=2.0, attempts=3)
FRAUD = "Please transfer EUR 5,000 to IBAN XX00 0000 0000 0000 to release your invoice."


def journal(**entry):
    with open("sender.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")
        f.flush()
        os.fsync(f.fileno())


def deliver(order: dict, text: str | None = None, certificate: list | None = None) -> dict:
    msg = Deliver(v=VERSION, who=ID, key=order["key"], signed_task=order["signed_task"],
                  text=order["text"] if text is None else text,
                  certificate=order["certificate"] if certificate is None else certificate)
    while True:                                   # the outcome must become known: keep asking
        try:
            return mail.call("/deliver", msg)
        except RPCError as e:
            return {"refused": e.payload.get("error", str(e))}
        except OSError:
            journal(task=order["task"], state="delivery_unknown")
            time.sleep(0.2)


def attack(order: dict) -> None:
    if BYZANTINE == "unapproved":
        out = deliver(order, text=FRAUD, certificate=[])
    elif BYZANTINE == "swap":
        out = deliver(order, text=FRAUD)
    else:
        return
    journal(task=order["task"], state="attack", attack=BYZANTINE, outcome=out)


def tell_ledger(path: str, message) -> None:
    while True:
        try:
            ledger.call(path, message)
            return
        except RPCError:
            return
        except OSError:
            time.sleep(0.5)


def main():
    attacked = False
    while True:
        try:
            order = ledger.call("/send/next", Msg(v=VERSION, who=ID))
        except (OSError, RPCError):
            time.sleep(0.5)
            continue
        if order.get("task") is None:
            if order.get("all_final"):
                journal(state="exit")
                return
            time.sleep(0.05)
            continue
        if not attacked and BYZANTINE:
            attacked = True
            attack(order)
        result = deliver(order)
        if result.get("refused"):
            journal(task=order["task"], state="refused", reason=result["refused"])
            tell_ledger("/send/refused", Refused(v=VERSION, who=ID, task=order["task"],
                                                 reason=result["refused"][:500]))
            continue
        journal(task=order["task"], state="delivered", resend=order["resend"],
                duplicate=result.get("duplicate", False))
        tell_ledger("/send/sent", Sent(v=VERSION, who=ID, task=order["task"]))


if __name__ == "__main__":
    main()
