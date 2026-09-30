"""The software task and the naive coordinator, in isolation. Run: python -m unittest -v test_coordinator"""

import unittest

from coordinator import Coordinator
from repo import Repo
from software import FIXES, INITIAL, REGRESSION, run_tests


def fixed(*task_ids):
    lib = INITIAL["src/lib.rs"]
    for t in task_ids:
        lib = FIXES[t](lib)
    return {**INITIAL, "src/lib.rs": lib}


class Software(unittest.TestCase):
    def test_initial_version_fails_both_bug_tests(self):
        self.assertEqual(run_tests(INITIAL)[1], ["fencing_rejects_stale", "grant_increments"])

    def test_each_fix_repairs_its_bug_and_both_repair_all(self):
        self.assertEqual(run_tests(fixed("T1"))[1], ["grant_increments"])
        self.assertEqual(run_tests(fixed("T2"))[1], ["fencing_rejects_stale"])
        self.assertEqual(run_tests(fixed("T1", "T2"))[1], [])

    def test_regression_undoes_the_lease_fix(self):
        lib = REGRESSION(fixed("T1", "T2")["src/lib.rs"])
        self.assertEqual(run_tests({"src/lib.rs": lib})[1], ["grant_increments"])


class NaiveBoard(unittest.TestCase):
    def test_claim_does_not_check_ownership(self):
        b = Coordinator(Repo(INITIAL))
        b.add_task("T1", "src/lib.rs", "")
        b.claim("T1", "impl-1")
        b.claim("T1", "impl-2")                                   # accepted without complaint
        self.assertEqual(b.tasks["T1"].owner, "impl-2")
        self.assertEqual(b.events[-1]["owner_before"], "impl-1")  # but the record shows it

    def test_accept_ignores_which_version_evidence_is_about(self):
        r = Repo(INITIAL)
        b = Coordinator(r)
        b.add_task("T1", "src/lib.rs", "")
        b.complete("T1", "impl-1", r.head, r.head)
        b.report("some-other-version", ["x"], [], "tester")
        b.review("yet-another", True, "reviewer")
        self.assertEqual(b.accept(), r.head)                      # accepted on evidence about other versions


if __name__ == "__main__":
    unittest.main()
