"""The audit: what the ledger should have done, rebuilt from evidence it does not control.

A lying ledger cannot make the mail service deliver what nobody approved, but it can do
nothing: drop votes, never approve, never hand an approved email to the sender. That is a
loss of liveness, and it is invisible from inside the ledger. The audit makes it visible,
and attributable, from what the participants hold:

  - every checker keeps its signed verdicts, and the receipt each front-end signed when it
    accepted one;
  - from the verdicts alone, the audit decides for each draft whether the checkers had
    certified it under the policy (the same rule the mail service applies);
  - it compares that with the ledger's state. A draft that was certified in the checkers'
    hands but is not approved in the ledger, with receipts showing which front-end
    accepted the missing votes, is a provable accusation: the receipts are signed, so the
    front-end cannot deny having answered "ok".

The audit decides nothing and repairs nothing; it says who to stop trusting.
"""

from __future__ import annotations

from collections import defaultdict

import evidence as E
import sig
from wire import receipt_doc


def audit(tasks: dict, journals: dict[str, list[dict]], checker_keys: dict[str, str],
          service_keys: dict[str, str], policy: dict) -> list[dict]:
    """tasks: the ledger's view {tid: {"state", "draft_hash", "voters"}}.
    journals: each checker's journal. Returns one finding per draft that the checkers
    certified but the ledger did not act on."""
    approvals = defaultdict(dict)        # (tid, draft_hash) -> {checker: receipt evidence}
    for checker, entries in journals.items():
        for e in entries:
            v = e.get("verdict")
            if not v or e.get("path") != "/verdict" or e.get("state") != "reported" or not v["approve"]:
                continue
            if not E.verdict_valid(v, checker_keys):
                continue
            r = e.get("receipt") or {}
            receipt_ok = bool(r) and r.get("service") in service_keys and sig.verify(
                service_keys[r["service"]], receipt_doc(r["service"], e.get("request", ""), r.get("status")),
                r.get("sig", ""))
            approvals[(e["task"], v["draft_hash"])][checker] = {"front_end": r.get("service"),
                                                                "receipt_valid": receipt_ok}
    findings = []
    for (tid, d_hash), by in sorted(approvals.items()):
        if not E.certifies(set(by), policy):
            continue
        t = tasks.get(tid)
        if t and t["state"] in ("APPROVED", "SENDING", "SENT"):
            continue
        if t and t.get("draft_hash") != d_hash and t["state"] != "UNDER_CHECK":
            continue                     # the ledger moved on legitimately (e.g. a lease ran out)
        missing = sorted(c for c in by if not t or c not in t.get("voters", []))
        accused = sorted({by[c]["front_end"] for c in missing if by[c]["receipt_valid"]})
        findings.append({"task": tid, "draft_hash": d_hash[:12], "certified_by": sorted(by),
                         "ledger_state": t["state"] if t else None,
                         "recorded_voters": t.get("voters", []) if t else [],
                         "acknowledged_but_missing": missing, "accused": accused})
    return findings
