"""A minimal client for etcd's v3 API over its HTTP/JSON gateway, standard library only.

etcd speaks gRPC; every etcd server also serves the same API as JSON under /v3/. Keys
and values are bytes and travel base64-encoded; 64-bit integers (revisions, lease ids,
TTLs) travel as strings; fields equal to their default (false, 0, empty) are omitted.

Any member accepts any request: a follower forwards writes to the leader, and reads are
linearizable by default. The client keeps a list of members and moves to the next one
when a member does not answer or answers "unavailable" (it is down, or the cluster is
electing a leader). A request that timed out may or may not have been applied: callers
make every write either conditional or idempotent, so that resending it is safe.

What is used here, and what it guarantees:
  get / scan         read keys; linearizable (reflects every write acknowledged before it)
  txn                if every comparison holds, apply the success operations, else the
                     failure ones; atomically, as one entry of the replicated log
  lease_grant        a lease with a time to live; keys attached to it are deleted, in one
                     committed change, when it expires or is revoked
  lease_keepalive    restart a lease's countdown; returns the remaining TTL (0: it is gone)
  watch              a stream of the changes to a range of keys, in revision order
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

UNAVAILABLE_CODES = {4, 13, 14}          # gRPC deadline exceeded, internal, unavailable


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def unb64(text: str | None) -> str:
    return base64.b64decode(text).decode() if text else ""


def prefix_end(prefix: str) -> str:
    """The first key after every key that starts with `prefix`."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


class EtcdError(Exception):
    """etcd answered, and refused the request."""


class Unavailable(EtcdError):
    """No member could serve the request (all down, or no leader at the moment)."""


class KV:
    __slots__ = ("key", "value", "mod_revision", "create_revision", "lease")

    def __init__(self, raw: dict):
        self.key = unb64(raw.get("key"))
        self.value = json.loads(unb64(raw.get("value")) or "null")
        self.mod_revision = int(raw.get("mod_revision", 0))
        self.create_revision = int(raw.get("create_revision", 0))
        self.lease = int(raw.get("lease", 0))


# --- building blocks of a transaction --------------------------------------------
def unchanged(kv: KV) -> dict:
    """Compare: the key was not modified since it was read (optimistic concurrency)."""
    return {"key": b64(kv.key), "target": "MOD", "result": "EQUAL", "mod_revision": str(kv.mod_revision)}


def absent(key: str) -> dict:
    """Compare: the key does not exist."""
    return {"key": b64(key), "target": "CREATE", "result": "EQUAL", "create_revision": "0"}


def put(key: str, value, lease: int = 0) -> dict:
    op = {"key": b64(key), "value": b64(json.dumps(value, sort_keys=True))}
    if lease:
        op["lease"] = str(lease)
    return {"request_put": op}


def delete(key: str) -> dict:
    return {"request_delete_range": {"key": b64(key)}}


class Etcd:
    def __init__(self, endpoints: list[str], timeout_s: float = 2.0):
        self.endpoints = list(endpoints)
        self.timeout_s = timeout_s
        self._at = 0                                   # the member that answered last

    def _post(self, path: str, body: dict, timeout_s: float | None = None) -> dict:
        last = "no endpoints"
        n = len(self.endpoints)
        for k in range(n):
            i = (self._at + k) % n
            req = urllib.request.Request(self.endpoints[i] + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as r:
                    self._at = i
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                try:
                    payload = json.loads(e.read() or b"{}")
                except ValueError:
                    payload = {}
                if e.code >= 500 or payload.get("code") in UNAVAILABLE_CODES:
                    last = f"{self.endpoints[i]}: {payload.get('message', e.code)}"
                    continue                           # this member cannot serve it now: try another
                raise EtcdError(f"{e.code}: {payload.get('message', payload)}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = f"{self.endpoints[i]}: {type(e).__name__}"
        raise Unavailable(last)

    # --- keys ----------------------------------------------------------------------
    def get(self, key: str) -> KV | None:
        kvs = self._post("/v3/kv/range", {"key": b64(key)}).get("kvs", [])
        return KV(kvs[0]) if kvs else None

    def scan(self, prefix: str) -> list[KV]:
        out = self._post("/v3/kv/range", {"key": b64(prefix), "range_end": b64(prefix_end(prefix))})
        return [KV(raw) for raw in out.get("kvs", [])]

    def txn(self, compare: list[dict], success: list[dict], failure: list[dict] = ()) -> bool:
        out = self._post("/v3/kv/txn", {"compare": compare, "success": success, "failure": list(failure)})
        return bool(out.get("succeeded", False))

    # --- leases ------------------------------------------------------------------
    def lease_grant(self, ttl_s: int) -> int:
        return int(self._post("/v3/lease/grant", {"TTL": str(ttl_s)})["ID"])

    def lease_keepalive(self, lease: int) -> int:
        out = self._post("/v3/lease/keepalive", {"ID": str(lease)})
        return int(out.get("result", out).get("TTL", 0))

    def lease_revoke(self, lease: int) -> None:
        try:
            self._post("/v3/lease/revoke", {"ID": str(lease)})
        except EtcdError:
            pass                                       # already expired or revoked: nothing to do

    # --- watching ------------------------------------------------------------------
    def watch(self, prefix: str, start_revision: int = 0, timeout_s: float = 3600.0):
        """Yield ("PUT" | "DELETE", key, revision) for every change under `prefix`, until
        the stream breaks (then the caller re-reads and watches again)."""
        create = {"key": b64(prefix), "range_end": b64(prefix_end(prefix))}
        if start_revision:
            create["start_revision"] = str(start_revision)
        last = "no endpoints"
        for k in range(len(self.endpoints)):
            i = (self._at + k) % len(self.endpoints)
            req = urllib.request.Request(self.endpoints[i] + "/v3/watch",
                                         data=json.dumps({"create_request": create}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                resp = urllib.request.urlopen(req, timeout=timeout_s)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = f"{self.endpoints[i]}: {type(e).__name__}"
                continue
            with resp:
                buf = b""
                while True:
                    chunk = resp.readline()
                    if not chunk:
                        return
                    buf += chunk
                    try:
                        msg = json.loads(buf)
                    except ValueError:
                        continue                       # a message split across lines
                    buf = b""
                    result = msg.get("result", {})
                    for ev in result.get("events", []):
                        kv = ev.get("kv", {})
                        yield ev.get("type", "PUT"), unb64(kv.get("key")), int(kv.get("mod_revision", 0))
            return
        raise Unavailable(last)

    # --- cluster -------------------------------------------------------------------
    def status(self, endpoint: str) -> dict | None:
        """One member's view: its id, the leader's id, its term. None if it does not answer."""
        req = urllib.request.Request(endpoint + "/v3/maintenance/status", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                out = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError):
            return None
        return {"member": out.get("header", {}).get("member_id"), "leader": out.get("leader"),
                "term": int(out.get("raftTerm", out.get("raft_term", 0)) or 0)}
