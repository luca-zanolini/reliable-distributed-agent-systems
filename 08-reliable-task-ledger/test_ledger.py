"""The ledger's rules, one decision per test. Fast: no network, no processes.

Run: python -m unittest test_ledger
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ledger import (APPROVED, CHECKING, FAILED, IN_PROGRESS, OPEN, SENDING, SENT, UNDER_CHECK,
                    Illegal, Journal, Ledger, Stale, fingerprint)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


BODY = {"to": "a@example.com", "subject": "Report 1"}


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.clock = Clock()
        self.l = self.open()
        self.l.add("T1", BODY)

    def open(self, **kw) -> Ledger:
        return Ledger(self.dir / "journal.jsonl", lease_s=2.0, max_attempts=3, clock=self.clock, **kw)

    def through_check(self, draft="Dear A, report 1."):
        g = self.l.claim("w1", "r1", "worker")
        self.l.submit("w1", "T1", g["token"], draft)
        c = self.l.claim("c1", "r2", "checker")
        return g, c

    # D1: the lifecycle --------------------------------------------------------
    def test_happy_path_reaches_sent(self):
        g, c = self.through_check()
        self.assertEqual(c["draft"], "Dear A, report 1.")
        self.l.verdict("c1", "T1", c["token"], fingerprint(c["draft"]), True, "ok")
        order = self.l.next_to_send()
        self.assertEqual((order["key"], order["text"], self.l.tasks["T1"]["state"]),
                         ("T1", "Dear A, report 1.", SENDING))
        self.l.mark_sent("T1")
        self.assertEqual(self.l.tasks["T1"]["state"], SENT)
        self.assertTrue(self.l.all_final())

    def test_no_skipping_the_check(self):
        self.assertIsNone(self.l.next_to_send()["task"])        # nothing approved, nothing sent
        with self.assertRaises(Illegal):
            self.l.mark_sent("T1")

    def test_reject_reopens_until_the_budget_is_spent(self):
        for attempt in range(1, 4):
            g = self.l.claim("w1", f"rw{attempt}", "worker")
            self.assertEqual(g["attempt"], attempt)
            self.l.submit("w1", "T1", g["token"], f"bad draft {attempt}")
            c = self.l.claim("c1", f"rc{attempt}", "checker")
            self.l.verdict("c1", "T1", c["token"], fingerprint(c["draft"]), False, "no greeting")
        self.assertEqual(self.l.tasks["T1"]["state"], FAILED)

    def test_permanent_failure_ends_at_once(self):
        g = self.l.claim("w1", "r1", "worker")
        self.l.give_up("w1", "T1", g["token"], permanent=True, reason="invalid address")
        self.assertEqual(self.l.tasks["T1"]["state"], FAILED)

    def test_transient_give_up_reopens(self):
        g = self.l.claim("w1", "r1", "worker")
        self.l.give_up("w1", "T1", g["token"], permanent=False, reason="timeout")
        self.assertEqual(self.l.tasks["T1"]["state"], OPEN)

    # D2: fencing --------------------------------------------------------------
    def test_epochs_grow_on_every_claim(self):
        a = self.l.claim("w1", "r1", "worker")
        self.clock.t = 3.0
        self.l.expire()
        b = self.l.claim("w2", "r2", "worker")
        self.assertEqual((a["token"], b["token"]), (1, 2))

    def test_zombie_submit_is_stale(self):
        a = self.l.claim("w1", "r1", "worker")
        self.clock.t = 3.0
        self.assertEqual(self.l.expire(), ["T1"])
        b = self.l.claim("w2", "r2", "worker")
        with self.assertRaises(Stale):
            self.l.submit("w1", "T1", a["token"], "late draft")
        with self.assertRaises(Stale):
            self.l.renew("w1", "T1", a["token"])
        self.l.submit("w2", "T1", b["token"], "current draft")
        self.assertEqual(self.l.tasks["T1"]["draft"], "current draft")

    def test_same_worker_reclaiming_gets_a_new_token(self):
        a = self.l.claim("w1", "r1", "worker")
        self.clock.t = 3.0
        self.l.expire()
        b = self.l.claim("w1", "r2", "worker")
        with self.assertRaises(Stale):
            self.l.submit("w1", "T1", a["token"], "old")
        self.l.submit("w1", "T1", b["token"], "new")

    def test_stale_after_expiry_even_before_anyone_reclaims(self):
        a = self.l.claim("w1", "r1", "worker")
        self.clock.t = 3.0
        self.l.expire()                                          # OPEN again, epoch unchanged
        with self.assertRaises(Stale):
            self.l.submit("w1", "T1", a["token"], "late")

    def test_epoch_survives_a_restart(self):
        self.l.claim("w1", "r1", "worker")
        self.clock.t = 3.0
        self.l.expire()
        restarted = self.open()
        self.assertEqual(restarted.claim("w2", "r2", "worker")["token"], 2)

    def test_saved_before_reply(self):
        seen = []
        self.l.after_save = lambda e: seen.append(Journal(self.dir / "journal.jsonl").replay()[-1])
        self.l.claim("w1", "r1", "worker")
        self.assertEqual(seen[0]["kind"], "claimed")             # on disk before the reply exists

    # D3: time -----------------------------------------------------------------
    def test_renewal_keeps_the_task(self):
        g = self.l.claim("w1", "r1", "worker")
        for t in (1.5, 3.0, 4.5):
            self.clock.t = t
            self.l.renew("w1", "T1", g["token"])
            self.assertEqual(self.l.expire(), [])

    def test_restart_grants_a_fresh_lease(self):
        g = self.l.claim("w1", "r1", "worker")
        self.clock.t = 100.0                                     # ledger was down a long time
        restarted = self.open()
        self.assertEqual(restarted.expire(), [])                 # not expired on arrival
        restarted.renew("w1", "T1", g["token"])                  # the owner can carry on

    # D4: the checker ----------------------------------------------------------
    def test_late_checker_is_stale(self):
        g, c1 = self.through_check()
        self.clock.t = 3.0
        self.l.expire()                                          # CHECKING -> UNDER_CHECK
        self.assertEqual(self.l.tasks["T1"]["state"], UNDER_CHECK)
        c2 = self.l.claim("c2", "r3", "checker")
        self.l.verdict("c2", "T1", c2["token"], fingerprint(c2["draft"]), False, "too short")
        with self.assertRaises(Stale):
            self.l.verdict("c1", "T1", c1["token"], fingerprint(c1["draft"]), True, "late ok")
        self.assertEqual(self.l.tasks["T1"]["state"], OPEN)

    def test_verdict_must_name_the_draft_under_check(self):
        g, c = self.through_check()
        with self.assertRaises(Illegal):
            self.l.verdict("c1", "T1", c["token"], fingerprint("another draft"), True, "ok")

    # D5: durability and repeats -------------------------------------------------
    def test_state_is_rebuilt_from_the_journal(self):
        g, c = self.through_check()
        self.l.verdict("c1", "T1", c["token"], fingerprint(c["draft"]), True, "ok")
        self.l.next_to_send()
        restarted = self.open()
        self.assertEqual(restarted.tasks["T1"]["state"], SENDING)
        order = restarted.next_to_send()                         # outcome unknown: resend
        self.assertEqual((order["key"], order["resend"]), ("T1", True))

    def test_approved_survives_a_restart_and_is_sent_once(self):
        g, c = self.through_check()
        self.l.verdict("c1", "T1", c["token"], fingerprint(c["draft"]), True, "ok")
        restarted = self.open()
        self.assertEqual(restarted.tasks["T1"]["state"], APPROVED)
        self.assertFalse(restarted.next_to_send()["resend"])     # never attempted before

    def test_torn_last_line_is_dropped(self):
        self.l.claim("w1", "r1", "worker")
        with open(self.dir / "journal.jsonl", "a") as f:
            f.write('{"crc": 1, "e": "{\\"kind\\": \\"subm')       # a crash mid-append
        restarted = self.open()
        self.assertEqual(restarted.tasks["T1"]["state"], IN_PROGRESS)
        restarted.add("T2", BODY)                                # and the journal is writable again
        self.assertIn("T2", self.open().tasks)

    def test_corruption_in_the_middle_is_not_ignored(self):
        self.l.claim("w1", "r1", "worker")
        lines = (self.dir / "journal.jsonl").read_text().splitlines()
        lines[0] = lines[0].replace("Report", "Rep0rt")
        (self.dir / "journal.jsonl").write_text("\n".join(lines) + "\n")
        with self.assertRaises(RuntimeError):
            self.open()

    def test_retried_claim_gets_the_same_grant(self):
        a = self.l.claim("w1", "r1", "worker")
        again = self.open().claim("w1", "r1", "worker")          # even across a restart
        self.assertEqual((again["task"], again["token"]), ("T1", a["token"]))
        self.assertEqual(len(Journal(self.dir / "journal.jsonl").replay()), 2)  # added + one claim

    def test_retried_submit_is_answered_not_reapplied(self):
        g = self.l.claim("w1", "r1", "worker")
        self.l.submit("w1", "T1", g["token"], "draft")
        self.l.claim("c1", "r2", "checker")                      # epoch moves on meanwhile
        self.assertTrue(self.l.submit("w1", "T1", g["token"], "draft")["duplicate"])
        with self.assertRaises(Stale):                           # a different draft is not a retry
            self.l.submit("w1", "T1", g["token"], "another draft")

    def test_mark_sent_twice_is_harmless(self):
        g, c = self.through_check()
        self.l.verdict("c1", "T1", c["token"], fingerprint(c["draft"]), True, "ok")
        self.l.next_to_send()
        self.l.mark_sent("T1")
        self.assertTrue(self.l.mark_sent("T1")["duplicate"])

    def test_the_ledger_refuses_illegal_transitions_from_its_own_code(self):
        with self.assertRaises(RuntimeError):
            self.l._apply({"kind": "sent", "task": "T1"})        # OPEN -> SENT is not an edge


if __name__ == "__main__":
    unittest.main()
