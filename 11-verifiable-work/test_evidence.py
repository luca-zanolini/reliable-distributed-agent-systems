"""The repository's judgement, offline: every way unproven work can arrive, and the honest case.

    python -m unittest test_evidence -v
"""

from __future__ import annotations

import unittest

import evidence as E
import project
import sig
from lab import rules


class World:
    def __init__(self, fencing="any", policy=None):
        self.issuer = sig.keypair()
        self.vkeys = {v: sig.keypair() for v in ("v1", "v2", "v3")}
        self.rkeys = {"human1": sig.keypair(), "bot1": sig.keypair()}
        self.policy = policy or E.threshold(1)
        self.pinned = {"issuer": self.issuer[1], "verifiers": {v: k[1] for v, k in self.vkeys.items()},
                       "policy": self.policy,
                       "reviewers": {"human1": {"pk": self.rkeys["human1"][1], "kind": "human"},
                                     "bot1": {"pk": self.rkeys["bot1"][1], "kind": "bot"}}}
        tests = {"tests/test_check.py": sig.digest(project.TEST_CHECK)}
        self.task = E.issue(self.issuer[0], "T1", {"title": "t", "tests": tests, "rules": rules(fencing)})
        self.base, self.result = "B" * 64, "R" * 64

    def att(self, v, outcome="pass", result=None, task=None):
        return E.attest(v, self.vkeys[v][0], task or self.task, self.base, result or self.result, "env", outcome, "rep")

    def rev(self, r, approve=True):
        return E.review(r, self.rkeys[r][0], self.task, self.base, self.result, approve)

    def check(self, changed, atts, revs=(), task=None):
        return E.check_commit(self.pinned, task or self.task, self.base, self.result, changed, list(atts), list(revs))


class Commit(unittest.TestCase):
    def test_tests_and_a_review_commit_a_fencing_change(self):
        w = World()
        self.assertIsNone(w.check(["fencing.py"], [w.att("v1"), w.att("v2")], [w.rev("human1")]))

    def test_a_changelog_change_needs_no_review(self):                     # experiment 8
        w = World()
        self.assertIsNone(w.check(["CHANGELOG.md"], [w.att("v1"), w.att("v2")]))

    def test_the_workers_own_word_is_not_evidence(self):                   # experiment 1
        w = World()
        own_key = sig.keypair()
        own = E.attest("w1", own_key[0], w.task, w.base, w.result, "my machine", "pass", "rep")
        why = w.check(["CHANGELOG.md"], [own])
        self.assertIn("w1: not a pinned verifier", why)

    def test_a_changed_acceptance_test_is_refused_even_with_passing_tests(self):  # experiment 2
        w = World()
        why = w.check(["fencing.py", "tests/test_check.py"], [w.att("v1"), w.att("v2")], [w.rev("human1")])
        self.assertEqual(why, "protected path changed: tests/test_check.py")

    def test_failing_outcomes_do_not_count(self):                           # experiments 3 and 4
        w = World()
        why = w.check(["CHANGELOG.md"], [w.att("v1"), w.att("v2", "fail"), w.att("v3", "fail")])
        self.assertIn("not certified", why)

    def test_flaky_is_not_pass(self):                                        # experiment 6
        w = World()
        self.assertIn("outcome flaky", w.check(["CHANGELOG.md"], [w.att("v1", "flaky"), w.att("v2", "flaky")]))

    def test_an_attestation_about_another_version_does_not_count(self):     # experiment 5
        w = World()
        why = w.check(["CHANGELOG.md"], [w.att("v1", result="X" * 64), w.att("v2", result="X" * 64)])
        self.assertIn("another task or version", why)

    def test_attested_tests_must_be_the_pinned_ones(self):
        w = World()
        other = {**w.task, "body": {**w.task["body"], "tests": {"tests/test_check.py": sig.digest("weaker")}}}
        forged = [E.attest(v, w.vkeys[v][0], other, w.base, w.result, "env", "pass", "rep") for v in ("v1", "v2")]
        self.assertIn("task not signed by the issuer", w.check(["CHANGELOG.md"], forged, task=other))

    def test_a_pass_on_other_tests_for_the_real_task_does_not_count(self):
        w = World()
        weaker = {"tests/test_check.py": sig.digest("weaker tests")}
        atts = []
        for v in ("v1", "v2"):
            doc = E.attestation_doc(E.task_hash(w.task), w.base, w.result, weaker, "env", "pass", "rep")
            atts.append({**doc, "verifier": v, "sig": sig.sign(w.vkeys[v][0], doc)})
        self.assertIn("ran other tests than the pinned ones", w.check(["CHANGELOG.md"], atts))

    def test_a_pass_in_a_verifiers_name_with_another_key_does_not_count(self):
        w = World()
        stranger = sig.keypair()[0]
        forged = [E.attest(v, stranger, w.task, w.base, w.result, "env", "pass", "rep") for v in ("v1", "v2")]
        self.assertIn("bad signature", w.check(["CHANGELOG.md"], forged))

    def test_a_fencing_change_without_review_is_refused(self):               # experiment 8b
        w = World()
        why = w.check(["CHANGELOG.md", "fencing.py"], [w.att("v1"), w.att("v2")])
        self.assertEqual(why, "review by a reviewer required for CHANGELOG.md, fencing.py: no valid approval")

    def test_a_rejecting_review_is_not_an_approval(self):                    # experiment 7
        w = World()
        self.assertIn("no valid approval", w.check(["fencing.py"], [w.att("v1"), w.att("v2")], [w.rev("human1", False)]))

    def test_a_review_in_a_reviewers_name_with_another_key_does_not_count(self):
        w = World()
        forged = E.review("human1", sig.keypair()[0], w.task, w.base, w.result, True)
        self.assertIn("no valid approval", w.check(["fencing.py"], [w.att("v1"), w.att("v2")], [forged]))

    def test_a_bot_approval_counts_where_any_reviewer_will_do(self):         # experiment 7c
        w = World(fencing="any")
        self.assertIsNone(w.check(["fencing.py"], [w.att("v1"), w.att("v2")], [w.rev("bot1")]))

    def test_a_bot_approval_does_not_count_where_a_human_is_required(self):  # experiment 7d
        w = World(fencing="human")
        self.assertIn("a human reviewer", w.check(["fencing.py"], [w.att("v1"), w.att("v2")], [w.rev("bot1")]))
        self.assertIsNone(w.check(["fencing.py"], [w.att("v1"), w.att("v2")], [w.rev("human1")]))

    def test_tests_only_rule_commits_without_review(self):                   # experiment 7b
        w = World(fencing="none")
        self.assertIsNone(w.check(["fencing.py"], [w.att("v1"), w.att("v2")]))

    def test_an_unnamed_path_needs_a_human(self):
        w = World()
        self.assertIn("a human reviewer", w.check(["setup.py"], [w.att("v1"), w.att("v2")], [w.rev("bot1")]))


class Rules(unittest.TestCase):
    def test_the_most_demanding_rule_applies(self):
        r = rules("any")
        self.assertEqual(E.required_review(["CHANGELOG.md"], r), "none")
        self.assertEqual(E.required_review(["CHANGELOG.md", "fencing.py"], r), "any")
        self.assertEqual(E.required_review(["fencing.py", "tests/test_check.py"], r), "forbidden")
        self.assertEqual(E.required_review([], r), "none")

    def test_changed_paths(self):
        self.assertEqual(E.changed_paths({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}), ["b", "c"])


if __name__ == "__main__":
    unittest.main()


class RepositoryGate(unittest.TestCase):
    """repo_service.Repository without HTTP: the base check and retried commits."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from repo_service import Repository
        self.w = World(fencing="none")
        self.dir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.dir.name), self.w.pinned, project.GENESIS)
        self.Repository, self.Path = Repository, Path

    def tearDown(self):
        self.dir.cleanup()

    def commit(self, repo, tid, base, files):
        from wire import Commit
        result = repo.store(files)
        task = E.issue(self.w.issuer[0], tid, self.w.task["body"])
        atts = [E.attest(v, self.w.vkeys[v][0], task, base, result, "env", "pass", "rep") for v in ("v1", "v2")]
        return repo.commit(Commit(v=1, who="k1", signed_task=task, base=base, result=result, attestations=atts,
                                  reviews=[]), "k1"), result

    def test_a_certified_change_on_an_old_head_is_refused(self):
        g = self.repo.head
        (code, _), r1 = self.commit(self.repo, "T1", g, project.change("T1", "honest", project.GENESIS))
        self.assertEqual((code, self.repo.head), (200, r1))
        (code, out), _ = self.commit(self.repo, "T2", g, project.change("T2", "honest", project.GENESIS))
        self.assertEqual(code, 422)
        self.assertTrue(out["error"].startswith("stale base"))
        self.assertEqual(self.repo.head, r1)

    def test_counterfactual_without_the_base_check_the_first_change_is_lost(self):
        repo = self.Repository(self.Path(self.dir.name), self.w.pinned, project.GENESIS, base_check=False)
        g = repo.head
        self.commit(repo, "T1", g, project.change("T1", "honest", project.GENESIS))
        (code, _), r2 = self.commit(repo, "T2", g, project.change("T2", "honest", project.GENESIS))
        self.assertEqual((code, repo.head), (200, r2))
        self.assertFalse(project.hidden_check_passes(repo.versions[repo.head]), "T1's fix is gone")

    def test_a_retried_commit_is_recognised(self):
        g = self.repo.head
        (code, _), r1 = self.commit(self.repo, "T1", g, project.change("T1", "honest", project.GENESIS))
        (code, out), _ = self.commit(self.repo, "T1", g, project.change("T1", "honest", project.GENESIS))
        self.assertEqual((code, out.get("duplicate")), (200, True))
