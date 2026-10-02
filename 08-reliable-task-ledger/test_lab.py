"""The stage's exit criterion on real processes (about 15 s): the four failures that
matter most, each must use its mechanism and keep every email delivered exactly once.

Run: python -m unittest test_lab
"""

from __future__ import annotations

import unittest

import experiments


class ExitCriterion(unittest.TestCase):
    def check(self, fn, *args):
        lab, mechanism, story = fn("proc", *args)
        try:
            ok, inv = experiments.invariant(lab)
        finally:
            lab.close()
        self.assertTrue(mechanism, story)
        return ok, inv

    def test_zombie_is_fenced(self):
        ok, inv = self.check(experiments.e2_zombie)
        self.assertTrue(ok, inv)

    def test_ledger_crash_between_save_and_reply(self):
        ok, inv = self.check(experiments.e5_ledger_crash_after_claim)
        self.assertTrue(ok, inv)

    def test_sender_crash_after_delivery(self):
        ok, inv = self.check(experiments.e6_sender_crash_after_send)
        self.assertTrue(ok, inv)

    def test_receiver_memory_is_what_prevents_duplicates(self):
        ok, inv = self.check(experiments.e7_mail_timeout)
        self.assertTrue(ok, inv)
        ok, inv = self.check(experiments.e7_mail_timeout, False)
        self.assertFalse(ok, f"without dedupe a duplicate was expected: {inv}")


if __name__ == "__main__":
    unittest.main()
