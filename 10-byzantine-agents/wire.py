"""Messages, request signatures and the retrying client.

Stage 9 sealed each request with an HMAC, using a secret the participant shared with the
service. Here each participant signs its requests with its own private key, and services
hold only public keys: a front-end can still check who is asking, but it can no longer
make a request that looks as if someone else had asked. A request is signed over
(participant, timestamp, body) and refused outside a replay window.

Services may also sign their answers (a receipt: "I, front-end f1, answered this request
with this status"), which a participant can keep as evidence of what it was told.

A request that times out may or may not have been processed, so the client retries with
exponential backoff and jitter, resending the same body, and moves to the next front-end
when one does not answer or cannot decide (503).
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

from pydantic import BaseModel, Field

import sig

VERSION = 1
REPLAY_WINDOW_S = 30


class Msg(BaseModel):
    v: int
    who: str


class Claim(Msg):
    request_id: str = Field(min_length=8, max_length=64)


class Renew(Msg):
    task: str
    token: int


class Submit(Renew):
    draft: str = Field(min_length=1, max_length=4000)


class GiveUp(Renew):
    permanent: bool
    reason: str = Field(max_length=200)


class AddTask(Msg):
    task: str
    body: dict
    issuer_sig: str


class Vote(Msg):
    task: str
    task_hash: str
    draft_hash: str
    approve: bool
    reason: str = Field(max_length=200)
    sig: str


class Sent(Msg):
    task: str


class Refused(Msg):
    task: str
    reason: str = Field(max_length=500)


class Deliver(Msg):
    key: str
    signed_task: dict
    text: str = Field(max_length=4000)
    certificate: list[dict]


def request_bytes(who: str, timestamp: str, body: bytes) -> bytes:
    return who.encode() + b"\n" + timestamp.encode() + b"\n" + body


def verify(registry: dict[str, dict], who: str, timestamp: str, signature: str,
           body: bytes) -> str | None:
    """Return None if the request is authentic, else the reason it is not."""
    if who not in registry:
        return "unknown participant"
    try:
        age = time.time() - float(timestamp)
    except ValueError:
        return "malformed timestamp"
    if abs(age) > REPLAY_WINDOW_S:
        return "timestamp outside the replay window"
    if not sig.verify_bytes(registry[who]["pk"], request_bytes(who, timestamp, body), signature):
        return "bad signature"
    return None


def receipt_doc(service: str, request_digest: str, status: int) -> dict:
    """What a service signs when it answers: which service, which request (by the hash of
    its body), which status."""
    return {"kind": "receipt", "service": service, "request": request_digest, "status": status}


class RPCError(Exception):
    def __init__(self, status: int, payload: dict):
        super().__init__(f"{status}: {payload}")
        self.status, self.payload = status, payload


class Client:
    def __init__(self, base_urls: str | list[str], who: str, secret_key: str, timeout_s: float = 2.0,
                 attempts: int = 8, base_backoff_s: float = 0.05, max_backoff_s: float = 1.0):
        urls = base_urls.split(",") if isinstance(base_urls, str) else list(base_urls)
        self.urls, self._at, self.who, self.sk = urls, 0, who, secret_key
        self.timeout_s, self.attempts = timeout_s, attempts
        self.base_backoff_s, self.max_backoff_s = base_backoff_s, max_backoff_s
        self.retries = 0

    def call(self, path: str, message: BaseModel) -> dict:
        body = json.dumps(message.model_dump()).encode()       # fixed once: retries resend it verbatim
        for attempt in range(self.attempts):
            ts = f"{time.time():.3f}"
            req = urllib.request.Request(
                self.urls[self._at] + path, data=body, method="POST",
                headers={"Content-Type": "application/json", "X-Who": self.who, "X-Timestamp": ts,
                         "X-Signature": sig.sign_bytes(self.sk, request_bytes(self.who, ts, body))})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    out = json.loads(resp.read())
                    out["_via"], out["_request"] = self.urls[self._at], sig.digest(body)
                    return out
            except urllib.error.HTTPError as e:
                payload = json.loads(e.read() or b"{}")
                if e.code != 503:                                # the server answered: no retry
                    raise RPCError(e.code, payload) from None
                if attempt == self.attempts - 1:
                    raise ConnectionError(f"unavailable: {payload}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == self.attempts - 1:
                    raise
            self.retries += 1
            self._at = (self._at + 1) % len(self.urls)           # this front-end failed us: try the next
            time.sleep(min(self.max_backoff_s, self.base_backoff_s * 2 ** attempt) * random.uniform(0.5, 1.5))
        raise AssertionError("unreachable")


def get_json(url: str, timeout_s: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            return json.loads(r.read())
    except OSError:
        return None
