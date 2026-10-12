"""End to end on a real etcd cluster (needs etcd on this host): the whole system, no
failures injected, then one etcd member killed mid-run. Every email exactly once, no
token given to two holders.

    python -m unittest test_lab -v
"""

from __future__ import annotations

import unittest

from experiments import finish, invariant
from lab import Lab


class EndToEnd(unittest.TestCase):
    def test_all_tasks_final_every_email_once_no_shared_token(self):
        lab = Lab("proc")
        try:
            s = finish(lab)
            ok, detail = invariant(lab)
            self.assertTrue(ok, detail)
            self.assertEqual(sorted(v["state"] for v in s["tasks"].values()), ["FAILED"] + ["SENT"] * 7)
        finally:
            lab.close()

    def test_a_follower_crash_changes_nothing(self):
        lab = Lab("proc")
        try:
            lab.wait(lambda: len(lab.grants()) >= 2, 60, "work under way")
            leader = lab.etcd.leader()
            follower = next(m for m in ("e1", "e2", "e3") if m != leader)
            lab.etcd.kill(follower)
            finish(lab)
            ok, detail = invariant(lab)
            self.assertTrue(ok, detail)
            self.assertEqual(lab.etcd.leader(), leader, "losing a follower does not need an election")
        finally:
            lab.close()


if __name__ == "__main__":
    unittest.main()
