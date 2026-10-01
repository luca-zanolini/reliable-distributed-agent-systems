"""The Stage 7 exit criterion, on real processes. Run: python -m unittest -v test_cluster
(about 15 seconds: each test starts a coordinator and three workers)"""

import unittest

import experiments


class ExitCriterion(unittest.TestCase):
    def test_baseline_every_job_exactly_once(self):
        r = experiments.baseline()
        self.assertEqual(set(r["completions"].values()), {1})
        self.assertEqual(r["duplicates"], 0)

    def test_killed_worker_is_detected_and_its_job_recovered(self):
        r = experiments.crash()
        self.assertTrue(r["reopened"])
        self.assertNotEqual(r["finished_by"], "w1")
        self.assertTrue(r["all_done"])
        self.assertLess(r["detection_latency_s"], 2.0)
        self.assertTrue(r["survivor_journals_consistent"])   # killing w1 did not corrupt w2, w3
        self.assertEqual(r["duplicates"], 0)

    def test_frozen_worker_becomes_a_zombie(self):
        r = experiments.freeze()
        self.assertTrue(r["suspicion_revised"])               # the detector was wrong: w1 was alive
        self.assertTrue(r["zombie_completion_refused"])       # the coordinator's record is protected
        self.assertEqual(r["coordinator_completions"], 1)
        self.assertEqual(r["external_effects_for_job"], 2)    # the external system is not: Stage 8

    def test_lost_replies_are_retried_idempotently(self):
        r = experiments.slow_replies()
        self.assertTrue(r["all_done"])
        self.assertGreater(r["retried_completions_ignored"], 0)
        self.assertEqual(r["max_completions_per_job"], 1)

    def test_forged_and_malformed_requests_are_rejected_at_the_border(self):
        r = experiments.forged()
        self.assertEqual(r["responses"], {"wrong_secret": 401, "unknown_worker": 401,
                                          "future_version": 400, "malformed": 400, "not_json": 400})
        self.assertEqual(r["max_completions_per_job"], 1)


if __name__ == "__main__":
    unittest.main()
