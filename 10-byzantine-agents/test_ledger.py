"""The ledger's rules and the front-end, offline, against an in-memory store.

    python -m unittest test_ledger -v
"""

from __future__ import annotations

import unittest

import evidence as E
import ledger as L
import sig
from audit import audit
from drafts import template
from fake_etcd import FakeEtcd
from frontend import HOLD, TASK, Frontend

CHECKERS = ["c1", "c2", "c3"]


class System:
    def __init__(self, tasks=2, policy=None, checkers=CHECKERS, byzantine=None, max_attempts=3):
        self.store = FakeEtcd()
        self.issuer = sig.keypair()
        self.keys = {who: sig.keypair() for who in ["w1", "w2", *checkers]}
        registry = {"i1": {"pk": self.issuer[1], "role": "issuer"},
                    **{w: {"pk": self.keys[w][1], "role": "worker"} for w in ("w1", "w2")},
                    **{c: {"pk": self.keys[c][1], "role": "checker"} for c in checkers}}
        self.policy = policy or E.threshold(1)
        self.fe = Frontend(self.store, registry, list(checkers), self.policy, max_attempts=max_attempts,
                           byzantine=byzantine)
        self.pinned = {"issuer": self.issuer[1], "checkers": {c: self.keys[c][1] for c in checkers},
                       "policy": self.policy}
        for i in range(1, tasks + 1):
            self.fe.add(E.issue(self.issuer[0], f"T{i}", {"to": f"c{i}@example.com", "subject": f"invoice #{i}"}))

    def task(self, tid):
        return self.store.get(TASK + tid).value

    def draft(self, tid="T1", worker="w1"):
        g = self.fe.claim(worker, f"req-{worker}-{tid}-{self.task(tid)['epoch']}")
        text = template(g["signed_task"]["body"]["subject"], worker)
        self.fe.submit(worker, g["task"], g["token"], text)
        return g["task"], text

    def vote(self, checker, tid, approve=True, text=None, key=None):
        t = self.task(tid)
        v = E.judge(checker, key or self.keys[checker][0], t["task"], text or t["draft"], approve)
        return self.fe.vote(checker, tid, v)


class Votes(unittest.TestCase):
    def test_f_plus_one_approvals_approve_with_a_certificate_the_mail_service_accepts(self):
        s = System()
        tid, text = s.draft()
        self.assertEqual(s.vote("c1", tid)["state"], L.UNDER_CHECK)
        self.assertEqual(s.vote("c2", tid)["state"], L.APPROVED)
        order = s.fe.next_to_send()
        self.assertEqual(order["text"], text)
        self.assertIsNone(E.check_delivery(s.pinned, order["signed_task"], order["text"], order["certificate"]))

    def test_once_approval_is_impossible_the_draft_goes_back(self):
        s = System()
        tid, _ = s.draft()
        s.vote("c1", tid, approve=False)
        self.assertEqual(s.vote("c2", tid, approve=False)["state"], L.OPEN, "c3 alone cannot certify any more")
        self.assertIn("rejected by c1, c2", s.task(tid)["reason"])

    def test_a_vote_on_an_earlier_draft_counts_for_nothing(self):
        s = System(max_attempts=5)
        tid, first = s.draft()
        s.vote("c1", tid, approve=False)
        s.vote("c2", tid, approve=False)                       # back to OPEN
        s.draft(worker="w2")                                   # a second, different draft
        with self.assertRaises(L.Stale):
            s.vote("c3", tid, approve=True, text=first)

    def test_a_repeated_vote_is_recognised_and_a_changed_vote_refused(self):
        s = System()
        tid, _ = s.draft()
        s.vote("c1", tid)
        self.assertTrue(s.vote("c1", tid)["duplicate"])
        with self.assertRaises(L.Illegal):
            s.vote("c1", tid, approve=False)

    def test_a_vote_not_signed_by_the_checker_is_refused(self):
        s = System()
        tid, _ = s.draft()
        with self.assertRaises(L.Illegal):
            s.vote("c1", tid, key=sig.keypair()[0])

    def test_the_fail_prone_policy_needs_the_independent_checker(self):
        s = System(policy=E.fail_prone({"l1", "l2", "l3"}, {"d1"}), checkers=["l1", "l2", "l3", "d1"])
        tid, _ = s.draft()
        for c in ("l1", "l2", "l3"):
            self.assertEqual(s.vote(c, tid)["state"], L.UNDER_CHECK)
        self.assertEqual(s.vote("d1", tid, approve=False)["state"], L.OPEN)

    def test_an_identical_resubmission_is_a_new_round_to_be_checked_afresh(self):
        s = System(tasks=1, max_attempts=5)
        tid, text = s.draft()
        first = s.fe.next_check("c1")
        s.vote("c1", tid, approve=False)
        s.vote("c2", tid, approve=False)                       # back to OPEN
        again, _ = s.draft()                                   # the same worker writes the same text
        second = s.fe.next_check("c1")
        self.assertEqual((first["draft_hash"], second["draft_hash"]), (s.task(tid)["draft_hash"],) * 2)
        self.assertNotEqual(first["round"], second["round"], "same text, new submission: vote again")

    def test_a_checker_is_offered_only_drafts_it_has_not_voted_on(self):
        s = System(tasks=1)
        tid, _ = s.draft()
        self.assertEqual(s.fe.next_check("c1")["task"], tid)
        s.vote("c1", tid)
        self.assertIsNone(s.fe.next_check("c1")["task"])
        self.assertEqual(s.fe.next_check("c2")["task"], tid)

    def test_a_task_not_signed_by_the_issuer_is_not_added_by_an_honest_front_end(self):
        s = System(tasks=0)
        with self.assertRaises(L.Illegal):
            s.fe.add(E.issue(sig.keypair()[0], "T9", {"to": "x@example.com", "subject": "s"}))

    def test_refused_is_final(self):
        s = System(tasks=1)
        tid, _ = s.draft()
        s.vote("c1", tid)
        s.vote("c2", tid)
        s.fe.next_to_send()
        s.fe.mark_refused(tid, "not certified")
        self.assertEqual(s.task(tid)["state"], L.REFUSED)
        self.assertTrue(s.fe.snapshot()["all_final"])


class Workers(unittest.TestCase):
    """Stage 9's guarantees for workers, still in place."""

    def test_a_zombie_is_refused_after_its_lease_ran_out(self):
        s = System(tasks=1)
        s.fe.claim("w1", "req-w1-0001")
        s.store.advance(3.1)
        self.assertEqual(s.fe.sweep(), ["T1"])
        g = s.fe.claim("w2", "req-w2-0001")
        self.assertEqual(g["token"], 2)
        with self.assertRaises(L.Stale):
            s.fe.submit("w1", "T1", 1, "late")

    def test_a_submit_releases_the_hold(self):
        s = System(tasks=1)
        s.draft()
        self.assertIsNone(s.store.get(HOLD + "T1"))
        self.assertEqual(s.task("T1")["state"], L.UNDER_CHECK)


class LyingFrontEnds(unittest.TestCase):
    def test_a_forging_front_end_approves_with_a_certificate_the_mail_service_refuses(self):
        s = System(tasks=1, byzantine="forge")
        tid, _ = s.draft()
        self.assertEqual(s.task(tid)["state"], L.APPROVED, "no checker ever voted")
        order = s.fe.next_to_send()
        why = E.check_delivery(s.pinned, order["signed_task"], order["text"], order["certificate"])
        self.assertIn("bad signature", why)

    def test_an_inventing_front_end_creates_a_task_the_mail_service_refuses(self):
        s = System(tasks=0)
        s.fe.invent(sig.keypair()[0])
        t = s.task("T99")
        self.assertEqual(t["task"]["body"]["to"], "attacker@evil.example")
        cert = [E.judge(c, s.keys[c][0], t["task"], "the list", True) for c in CHECKERS]
        self.assertEqual(E.check_delivery(s.pinned, t["task"], "the list", cert), "task not signed by the issuer")

    def test_a_dropping_front_end_acknowledges_and_records_nothing_and_the_audit_names_it(self):
        s = System(tasks=1, byzantine="drop-votes")
        tid, text = s.draft()
        fe_key = sig.keypair()
        from wire import receipt_doc
        journals = {}
        for c in ("c1", "c2"):
            v = E.judge(c, s.keys[c][0], s.task(tid)["task"], text, True)
            self.assertEqual(s.fe.vote(c, tid, v), {"ok": True})
            request = sig.digest(f"vote {c}")
            receipt = {"service": "f1", "status": 200, "sig": sig.sign(fe_key[0], receipt_doc("f1", request, 200))}
            journals[c] = [{"task": tid, "path": "/verdict", "state": "reported", "verdict": v,
                            "receipt": receipt, "request": request}]
        self.assertEqual(s.task(tid)["state"], L.UNDER_CHECK)
        self.assertEqual(s.task(tid)["votes"], {})
        findings = audit(s.fe.snapshot()["tasks"], journals, {c: s.keys[c][1] for c in CHECKERS},
                         {"f1": fe_key[1]}, s.policy)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["accused"], ["f1"])

    def test_the_audit_accuses_no_one_for_a_draft_the_checkers_had_not_certified(self):
        s = System(tasks=1, byzantine="drop-votes")
        tid, text = s.draft()
        fe_key = sig.keypair()
        from wire import receipt_doc
        v = E.judge("c1", s.keys["c1"][0], s.task(tid)["task"], text, True)
        s.fe.vote("c1", tid, v)
        request = sig.digest("vote c1")
        receipt = {"service": "f1", "status": 200, "sig": sig.sign(fe_key[0], receipt_doc("f1", request, 200))}
        journals = {"c1": [{"task": tid, "path": "/verdict", "state": "reported", "verdict": v,
                            "receipt": receipt, "request": request}]}
        self.assertEqual(audit(s.fe.snapshot()["tasks"], journals, {c: s.keys[c][1] for c in CHECKERS},
                               {"f1": fe_key[1]}, s.policy), [], "one approval is not yet a certificate")

    def test_the_audit_accuses_no_one_when_the_ledger_did_its_job(self):
        s = System(tasks=1)
        tid, text = s.draft()
        journals = {}
        for c in ("c1", "c2"):
            v = E.judge(c, s.keys[c][0], s.task(tid)["task"], text, True)
            s.fe.vote(c, tid, v)
            journals[c] = [{"task": tid, "path": "/verdict", "state": "reported", "verdict": v}]
        self.assertEqual(audit(s.fe.snapshot()["tasks"], journals, {c: s.keys[c][1] for c in CHECKERS}, {},
                               s.policy), [])


if __name__ == "__main__":
    unittest.main()
