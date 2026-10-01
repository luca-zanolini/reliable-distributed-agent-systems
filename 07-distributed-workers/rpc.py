"""The client side of RPC: signed requests, timeouts, retries with backoff.

A request that times out may or may not have been processed: the request can be
lost on the way, or the reply on the way back. The client cannot tell the two
apart, so it retries, and every operation it retries must be idempotent on the
server (`complete` is; see coordinator_service.py). Retries back off exponentially
with random jitter, so that many clients failing together do not retry in lockstep.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

from protocol import encode, headers


class RPCError(Exception):
    def __init__(self, status: int, payload: dict):
        super().__init__(f"{status}: {payload}")
        self.status, self.payload = status, payload


class Client:
    def __init__(self, base_url: str, worker: str, secret: str, timeout_s: float = 0.5,
                 attempts: int = 5, base_backoff_s: float = 0.05):
        self.base_url, self.worker, self.secret = base_url, worker, secret
        self.timeout_s, self.attempts, self.base_backoff_s = timeout_s, attempts, base_backoff_s
        self.retries = 0

    def call(self, path: str, message) -> dict:
        body = encode(message)
        for attempt in range(self.attempts):
            req = urllib.request.Request(self.base_url + path, data=body, method="POST",
                                         headers=headers(self.secret, self.worker, body))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:              # the server answered: do not retry
                raise RPCError(e.code, json.loads(e.read() or b"{}")) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == self.attempts - 1:
                    raise
                self.retries += 1
                time.sleep(self.base_backoff_s * 2 ** attempt * random.uniform(0.5, 1.5))
        raise AssertionError("unreachable")
