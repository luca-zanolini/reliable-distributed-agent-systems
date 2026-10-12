"""The ledger's rules and the front-end's use of the store, offline and deterministic.

FakeEtcd stands in for etcd: the same operations (get, scan, conditional transactions,
leases that delete their keys when time runs out) with a manual clock and hooks to
inject the awkward cases: a write applied whose reply is lost, and another front-end
committing between our read and our write. The real cluster is in test_lab.py.

    python -m unittest test_ledger -v
"""

from __future__ import annotations

import json
import unittest

import ledger as L
from etcd import KV, Unavailable, b64, unb64
from frontend import CLAIM, HOLD, TASK, Frontend


class FakeEtcd:
    def __init__(self):
        self.kv: dict[str, tuple] = {}        # key -> (value, mod_rev, create_rev, lease)
        self.rev = 1
        self.leases: dict[int, dict] = {}     # id -> {"ttl", "deadline"}
        self.now = 0.0
        self.next_lease = 100
        self.down = False
        self.before_txn = None                # hook(store): runs just before a txn is evaluated
        self.lose_next_reply = False          # apply the next successful txn, then raise
        self.revoked: list[int] = []

    def _check(self):
        if self.down:
            raise Unavailable("fake: no majority")

    def _kv(self, key):
        v = self.kv[key]
        return KV({"key": b64(key), "value": b64(json.dumps(v[0])), "mod_revision": str(v[1]),
                   "create_revision": str(v[2]), "lease": str(v[3])})

    def get(self, key):
        self._check()
        return self._kv(key) if key in self.kv else None

    def scan(self, prefix):
        self._check()
        return [self._kv(k) for k in sorted(self.kv) if k.startswith(prefix)]

    def _holds(self, c) -> bool:
        key = unb64(c["key"])
        if c["target"] == "MOD":
            return key in self.kv and self.kv[key][1] == int(c["mod_revision"])
        if c["target"] == "CREATE":
            return (self.kv[key][2] if key in self.kv else 0) == int(c["create_revision"])
        raise NotImplementedError(c)

    def txn(self, compare, success, failure=()):
        self._check()
        if self.before_txn:
            hook, self.before_txn = self.before_txn, None
            hook(self)
        ok = all(self._holds(c) for c in compare)
        if ok:
            self.rev += 1
            for op in success:
                if "request_put" in op:
                    p = op["request_put"]
                    key, value = unb64(p["key"]), json.loads(unb64(p["value"]))
                    created = self.kv[key][2] if key in self.kv else self.rev
                    self.kv[key] = (value, self.rev, created, int(p.get("lease", 0)))
                else:
                    self.kv.pop(unb64(op["request_delete_range"]["key"]), None)
            if self.lose_next_reply:
                self.lose_next_reply = False
                raise Unavailable("fake: committed, but the reply was lost")
        return ok

    def lease_grant(self, ttl_s):
        self._check()
        self.next_lease += 1
        self.leases[self.next_lease] = {"ttl": ttl_s, "deadline": self.now + ttl_s}
        return self.next_lease

    def lease_keepalive(self, lease):
        self._check()
        if lease not in self.leases:
            return 0
        self.leases[lease]["deadline"] = self.now + self.leases[lease]["ttl"]
        return self.leases[lease]["ttl"]

    def lease_revoke(self, lease):
        self.revoked.append(lease)
        self._drop(lease)

    def _drop(self, lease):
        self.leases.pop(lease, None)
        for k in [k for k, v in self.kv.items() if v[3] == lease]:
            del self.kv[k]
            self.rev += 1

    def advance(self, seconds):
        self.now += seconds
        for lease in [i for i, l in self.leases.items() if l["deadline"] <= self.now]:
            self._drop(lease)


def system(blind=False, tasks=3, max_attempts=3):
    store = FakeEtcd()
    fe = Frontend(store, lease_s=3.0, max_attempts=max_attempts, blind=blind)
    for i in range(1, tasks + 1):
        fe.add(f"T{i}", {"to": f"c{i}@example.com", "subject": f"Report {i}"})
    return store, fe


def task(store, tid):
    return store.get(TASK + tid).value


class Lifecycle(unittest.TestCase):
    def test_a_task_goes_from_open_to_sent(self):
        store, fe = system(tasks=1)
        g = fe.claim("w1", "req-w1-0001", "worker")
        self.assertEqual((g["task"], g["token"]), ("T1", 1))
        self.assertIsNotNone(store.get(HOLD + "T1"))
        fe.submit("w1", "T1", 1, "Dear customer, Report 1")
        self.assertEqual(task(store, "T1")["state"], L.UNDER_CHECK)
        self.assertIsNone(store.get(HOLD + "T1"), "the hold is released with the submit")
        c = fe.claim("c1", "req-c1-0001", "checker")
        self.assertEqual((c["task"], c["token"]), ("T1", 2))
        fe.verdict("c1", "T1", 2, L.fingerprint("Dear customer, Report 1"), True, "ok")
        order = fe.next_to_send()
        self.assertEqual((order["task"], order["resend"]), ("T1", False))
        self.assertEqual(task(store, "T1")["state"], L.SENDING, "the intent is recorded before the order leaves")
        fe.mark_sent("T1")
        self.assertEqual(task(store, "T1")["state"], L.SENT)
        self.assertTrue(fe.snapshot()["all_final"])

    def test_an_unfinished_send_is_handed_out_again_with_the_same_key(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        fe.submit("w1", "T1", 1, "Dear customer, Report 1")
        fe.claim("c1", "req-c1-0001", "checker")
        fe.verdict("c1", "T1", 2, L.fingerprint("Dear customer, Report 1"), True, "ok")
        first, again = fe.next_to_send(), fe.next_to_send()
        self.assertEqual((again["task"], again["key"], again["resend"]), (first["task"], first["key"], True))
        fe.mark_sent("T1")
        self.assertTrue(fe.mark_sent("T1")["duplicate"])

    def test_a_permanent_give_up_fails_the_task_and_releases_the_hold(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        out = fe.give_up("w1", "T1", 1, True, "invalid address")
        self.assertEqual(out["state"], L.FAILED)
        self.assertIsNone(store.get(HOLD + "T1"))


class Fencing(unittest.TestCase):
    def test_a_zombie_is_refused_after_its_lease_ran_out_and_the_task_moved_on(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        store.advance(3.1)                                  # w1 stopped renewing
        self.assertIsNone(store.get(HOLD + "T1"), "etcd deleted the hold with the lease")
        self.assertEqual(fe.sweep(), ["T1"])
        g = fe.claim("w2", "req-w2-0001", "worker")
        self.assertEqual(g["token"], 2)
        with self.assertRaises(L.Stale):
            fe.submit("w1", "T1", 1, "Dear customer, late")
        with self.assertRaises(L.Stale):
            fe.renew("w1", "T1", 1)
        self.assertEqual(fe.counters["stale_refused"], 2)

    def test_a_renewal_after_the_lease_ran_out_is_stale_even_before_the_task_is_reopened(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        store.advance(3.1)
        with self.assertRaises(L.Stale):
            fe.renew("w1", "T1", 1)

    def test_renewing_keeps_the_task(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        for _ in range(5):
            store.advance(2.0)
            fe.renew("w1", "T1", 1)
        self.assertEqual(fe.sweep(), [])
        self.assertEqual(task(store, "T1")["state"], L.IN_PROGRESS)

    def test_an_expired_check_goes_back_to_be_checked_not_redone(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        fe.submit("w1", "T1", 1, "Dear customer, Report 1")
        fe.claim("c1", "req-c1-0001", "checker")
        store.advance(3.1)
        fe.sweep()
        self.assertEqual(task(store, "T1")["state"], L.UNDER_CHECK)

    def test_the_attempt_budget_ends_in_failed(self):
        store, fe = system(tasks=1, max_attempts=2)
        for n in range(2):
            fe.claim(f"w{n}", f"req-w{n}-0001", "worker")
            store.advance(3.1)
            fe.sweep()
        self.assertEqual(task(store, "T1")["state"], L.FAILED)

    def test_an_expiry_request_for_a_task_still_held_does_nothing(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        real_get = store.get
        store.get = lambda key: None if key == HOLD + "T1" else real_get(key)   # a stale view of the hold
        self.assertFalse(fe.expire("T1"), "the write is conditional on the hold being gone")
        store.get = real_get
        self.assertEqual(task(store, "T1")["state"], L.IN_PROGRESS)

    def test_the_hold_is_released_in_the_same_write_even_if_the_lease_cannot_be_revoked(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        store.lease_revoke = lambda lease: None                               # the revoke is lost
        fe.submit("w1", "T1", 1, "Dear customer, Report 1")
        self.assertIsNone(store.get(HOLD + "T1"))

    def test_a_reported_expiry_of_a_task_already_reclaimed_does_nothing(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        store.advance(3.1)
        fe.sweep()
        fe.claim("w2", "req-w2-0001", "worker")
        self.assertFalse(fe.expire("T1"), "a late deletion event must not reopen the new holder's task")
        self.assertEqual(task(store, "T1")["owner"], "w2")


class Repeats(unittest.TestCase):
    def test_a_retried_claim_gets_the_same_task_and_token_from_any_front_end(self):
        store, fe = system()
        g = fe.claim("w1", "req-w1-0001", "worker")
        other = Frontend(store, lease_s=3.0)
        again = other.claim("w1", "req-w1-0001", "worker")
        self.assertEqual((again["task"], again["token"]), (g["task"], g["token"]))
        self.assertEqual(other.counters["claim_replayed"], 1)

    def test_a_claim_committed_whose_reply_was_lost_is_recognised_on_retry(self):
        store, fe = system()
        store.lose_next_reply = True
        with self.assertRaises(Unavailable):
            fe.claim("w1", "req-w1-0001", "worker")
        again = fe.claim("w1", "req-w1-0001", "worker")
        self.assertEqual((again["task"], again["token"]), ("T1", 1))
        self.assertIsNotNone(store.get(HOLD + "T1"), "the hold survived: the retry is the same claim")

    def test_a_resent_claim_that_fails_its_condition_does_not_revoke_its_own_lease(self):
        # The client resent a committed write to another member: the resend fails its
        # condition. The front-end must find its own claim, not give the lease back.
        store, fe = system()
        real_txn = store.txn

        def committed_then_false(compare, success, failure=()):
            real_txn(compare, success, failure)
            return False
        store.txn = committed_then_false
        g = fe.claim("w1", "req-w1-0001", "worker")
        store.txn = real_txn
        self.assertEqual((g["task"], g["token"]), ("T1", 1))
        self.assertEqual(store.revoked, [])
        self.assertIsNotNone(store.get(HOLD + "T1"))

    def test_repeated_submit_and_verdict_are_answered_not_reapplied(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        fe.submit("w1", "T1", 1, "Dear customer, Report 1")
        self.assertTrue(fe.submit("w1", "T1", 1, "Dear customer, Report 1")["duplicate"])
        fe.claim("c1", "req-c1-0001", "checker")
        h = L.fingerprint("Dear customer, Report 1")
        fe.verdict("c1", "T1", 2, h, True, "ok")
        self.assertTrue(fe.verdict("c1", "T1", 2, h, True, "ok")["duplicate"])

    def test_a_verdict_must_name_the_draft_being_checked(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        fe.submit("w1", "T1", 1, "Dear customer, Report 1")
        fe.claim("c1", "req-c1-0001", "checker")
        with self.assertRaises(L.Illegal):
            fe.verdict("c1", "T1", 2, L.fingerprint("another draft"), True, "ok")


class Races(unittest.TestCase):
    def test_two_front_ends_never_grant_the_same_task_twice(self):
        store, f1 = system(tasks=2)
        f2 = Frontend(store, lease_s=3.0)
        won = {}
        # f2 commits its claim between f1's read and f1's write
        store.before_txn = lambda s: won.update(f2=f2.claim("w2", "req-w2-0001", "worker"))
        g1 = f1.claim("w1", "req-w1-0001", "worker")
        g2 = won["f2"]
        self.assertEqual((g2["task"], g2["token"]), ("T1", 1))
        self.assertEqual((g1["task"], g1["token"]), ("T2", 1), "f1's condition failed; it re-read and took T2")
        self.assertEqual(f1.counters["conflicts"], 1)
        self.assertEqual(task(store, "T1")["owner"], "w2")
        self.assertTrue(store.revoked, "the lease of the losing attempt is revoked")

    def test_two_submits_racing_one_wins_the_other_is_answered_as_a_repeat(self):
        store, fe = system(tasks=1)
        fe.claim("w1", "req-w1-0001", "worker")
        other = Frontend(store, lease_s=3.0)
        store.before_txn = lambda s: other.submit("w1", "T1", 1, "Dear customer, Report 1")
        out = fe.submit("w1", "T1", 1, "Dear customer, Report 1")
        self.assertTrue(out["duplicate"])
        self.assertEqual(task(store, "T1")["state"], L.UNDER_CHECK)

    def test_counterfactual_blind_writes_hand_the_same_token_to_two_workers(self):
        store, f1 = system(blind=True, tasks=2)
        f2 = Frontend(store, lease_s=3.0, blind=True)
        won = {}
        store.before_txn = lambda s: won.update(f2=f2.claim("w2", "req-w2-0001", "worker"))
        g1 = f1.claim("w1", "req-w1-0001", "worker")
        g2 = won["f2"]
        self.assertEqual((g1["task"], g1["token"]), (g2["task"], g2["token"]),
                         "without the condition, both believe they hold T1 with token 1")
        self.assertEqual(task(store, "T1")["owner"], "w1", "and the store remembers only the last writer")


class Availability(unittest.TestCase):
    def test_without_a_majority_nothing_is_decided(self):
        store, fe = system()
        store.down = True
        with self.assertRaises(Unavailable):
            fe.claim("w1", "req-w1-0001", "worker")
        store.down = False
        g = fe.claim("w1", "req-w1-0001", "worker")
        self.assertEqual(g["token"], 1, "the failed attempt left no trace")


if __name__ == "__main__":
    unittest.main()
