"""The ledger's rules and the front-end, offline, against an in-memory store.

    python -m unittest test_ledger -v
"""

from __future__ import annotations

import unittest

import evidence as E
import ledger as L
import project
import sig
from fake_etcd import FakeEtcd
from frontend import TASK, Frontend
from lab import rules

VERIFIERS = ["v1", "v2", "v3"]


class System:
    def __init__(self, fencing="any", reviewers=None, byzantine=None, max_attempts=3, tasks=("T1",)):
        self.store = FakeEtcd()
        self.issuer = sig.keypair()
        self.reviewers = reviewers or {"r1": "human"}
        self.keys = {who: sig.keypair() for who in ["w1", *VERIFIERS, *self.reviewers]}
        registry = {"i1": {"pk": self.issuer[1], "role": "issuer"}, "w1": {"pk": self.keys["w1"][1], "role": "worker"},
                    **{v: {"pk": self.keys[v][1], "role": "verifier"} for v in VERIFIERS},
                    **{r: {"pk": self.keys[r][1], "role": "reviewer"} for r in self.reviewers}}
        self.changes = {}                                        # (base, result) -> changed paths
        self.fe = Frontend(self.store, registry, VERIFIERS, self.reviewers, E.threshold(1),
                           lambda b, r: self.changes.get((b, r)), max_attempts=max_attempts, byzantine=byzantine)
        for tid in tasks:
            spec = project.TASKS[tid]
            body = {"title": spec["title"], "tests": {p: sig.digest(project.GENESIS[p]) for p in spec["tests"]},
                    "rules": rules(fencing)}
            self.fe.add(E.issue(self.issuer[0], tid, body))

    def task(self, tid="T1"):
        return self.store.get(TASK + tid).value

    def submit(self, tid="T1", base="B", result="R", changed=("fencing.py",)):
        self.changes[(base, result)] = list(changed)
        g = self.fe.claim("w1", f"req-{tid}-{self.task(tid)['epoch']}-{result}")
        return self.fe.submit("w1", tid, g["token"], base, result)

    def attest(self, v, outcome="pass", tid="T1"):
        t = self.task(tid)
        a = E.attest(v, self.keys[v][0], t["task"], t["base"], t["result"], "env", outcome, "rep")
        return self.fe.attest(v, tid, a)

    def review(self, r, approve=True, tid="T1"):
        t = self.task(tid)
        return self.fe.review(r, tid, E.review(r, self.keys[r][0], t["task"], t["base"], t["result"], approve))


class Decisions(unittest.TestCase):
    def test_passing_tests_and_a_review_approve(self):
        s = System()
        self.assertEqual(s.submit()["review"], "any")
        s.attest("v1")
        self.assertEqual(s.attest("v2")["state"], L.UNDER_CHECK, "the review is still missing")
        self.assertEqual(s.review("r1")["state"], L.APPROVED)
        order = s.fe.next_commit()
        self.assertEqual(len(order["attestations"]), 2)
        self.assertEqual(len(order["reviews"]), 1)

    def test_a_changelog_change_is_approved_on_tests_alone(self):
        s = System(tasks=("T3",))
        self.assertEqual(s.submit("T3", changed=["CHANGELOG.md"])["review"], "none")
        s.attest("v1", tid="T3")
        self.assertEqual(s.attest("v2", tid="T3")["state"], L.APPROVED)

    def test_certified_failure_sends_the_work_back(self):
        s = System()
        s.submit()
        s.attest("v1")                                           # a liar says pass
        s.attest("v2", "fail")
        self.assertEqual(s.attest("v3", "fail")["state"], L.OPEN)
        self.assertIn("tests fail", s.task()["reason"])

    def test_one_liar_cannot_approve(self):
        s = System(fencing="none")
        s.submit()
        self.assertEqual(s.attest("v1")["state"], L.UNDER_CHECK)

    def test_flaky_tests_stop_at_needs_human(self):
        s = System()
        s.submit()
        s.attest("v1", "flaky")
        self.assertEqual(s.attest("v2", "flaky")["state"], L.NEEDS_HUMAN)
        self.assertIn("nondeterministic", s.task()["reason"], "the human must be told why")

    def test_a_certified_pass_and_a_certified_failure_prove_nondeterminism(self):
        s = System(fencing="any")
        s.submit()
        s.attest("v1", "pass")
        s.attest("v2", "fail")
        s.attest("v3", "pass")                                   # passes certified, failures not: approval waits for review
        self.assertEqual(s.task()["state"], L.UNDER_CHECK)
        s2 = System(fencing="any")
        s2.submit()
        for v, o in (("v1", "pass"), ("v2", "fail"), ("v3", "flaky")):
            s2.attest(v, o)
        self.assertEqual(s2.task()["state"], L.NEEDS_HUMAN)

    def test_a_human_rejection_sends_the_work_back(self):
        s = System(fencing="human", reviewers={"bot1": "bot", "human1": "human"})
        s.submit()
        s.attest("v1")
        s.attest("v2")
        s.review("bot1", True)                                   # does not qualify for "human"
        self.assertEqual(s.task()["state"], L.UNDER_CHECK)
        self.assertEqual(s.review("human1", False)["state"], L.OPEN)

    def test_evidence_about_an_earlier_submission_is_stale(self):
        s = System(max_attempts=5)
        s.submit(result="R1")
        old = s.task()
        s.attest("v1", "fail")
        s.attest("v2", "fail")                                   # back to OPEN
        s.submit(result="R2")
        late = E.attest("v3", s.keys["v3"][0], old["task"], old["base"], "R1", "env", "pass", "rep")
        with self.assertRaises(L.Stale):
            s.fe.attest("v3", "T1", late)

    def test_an_attestation_signed_by_someone_else_is_refused(self):
        s = System()
        s.submit()
        t = s.task()
        a = E.attest("v1", sig.keypair()[0], t["task"], t["base"], t["result"], "env", "pass", "rep")
        with self.assertRaises(L.Illegal):
            s.fe.attest("v1", "T1", a)

    def test_a_stale_base_reopens_and_any_other_refusal_is_final(self):
        for reason, state in (("stale base: moved on", L.OPEN), ("protected path changed: tests/x", L.REFUSED)):
            s = System(fencing="none")
            s.submit()
            s.attest("v1")
            s.attest("v2")
            s.fe.next_commit()
            s.fe.mark_refused("T1", reason)
            self.assertEqual(s.task()["state"], state, reason)

    def test_a_skipping_front_end_routes_nothing_to_reviewers(self):
        s = System(byzantine="skip-review", tasks=("T3",))
        self.assertEqual(s.submit("T3", changed=["CHANGELOG.md", "fencing.py"])["review"], "none")
        s.attest("v1", tid="T3")
        self.assertEqual(s.attest("v2", tid="T3")["state"], L.APPROVED, "the ledger lets it through; the repository will not")

    def test_reviewers_and_verifiers_are_offered_only_what_they_have_not_done(self):
        s = System()
        s.submit()
        self.assertEqual(s.fe.next_check("v1")["task"], "T1")
        s.attest("v1")
        self.assertIsNone(s.fe.next_check("v1")["task"])
        self.assertEqual(s.fe.next_review("r1")["task"], "T1")


class Project(unittest.TestCase):
    """The changes really behave as the experiments assume, under the real test runner."""

    def pinned(self, tid):
        return {p: project.GENESIS[p] for p in project.TASKS[tid]["tests"]}

    def test_honest_fixes_pass_and_the_starting_code_fails(self):
        for tid in ("T1", "T2", "T3"):
            self.assertFalse(project.run_tests(project.GENESIS, self.pinned(tid))[0][0], tid)
            self.assertTrue(project.run_tests(project.change(tid, "honest", project.GENESIS), self.pinned(tid))[0][0], tid)

    def test_the_cheat_passes_the_pinned_tests_and_fails_the_hidden_ones(self):
        files = project.change("T1", "cheat", project.GENESIS)
        self.assertTrue(project.run_tests(files, self.pinned("T1"))[0][0])
        self.assertFalse(project.hidden_check_passes(files))
        self.assertTrue(project.hidden_check_passes(project.change("T1", "honest", project.GENESIS)))

    def test_claim_pass_changes_nothing_that_matters(self):
        self.assertFalse(project.run_tests(project.change("T1", "claim-pass", project.GENESIS), self.pinned("T1"))[0][0])

    def test_edit_tests_weakens_the_workers_copy_but_the_pinned_tests_still_run(self):
        files = project.change("T1", "edit-tests", project.GENESIS)
        self.assertNotEqual(files["tests/test_check.py"], project.TEST_CHECK)
        self.assertTrue(project.run_tests(files, self.pinned("T1"))[0][0])


if __name__ == "__main__":
    unittest.main()
