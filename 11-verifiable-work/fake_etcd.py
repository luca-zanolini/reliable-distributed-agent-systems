"""In-memory stand-in for etcd (Stage 9's FakeEtcd, unchanged): the same operations (get,
scan, conditional transactions, leases that delete their keys when time runs out) with a
manual clock and hooks for the awkward cases. Shared by the offline tests."""

from __future__ import annotations

import json

from etcd import KV, Unavailable, b64, unb64


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


