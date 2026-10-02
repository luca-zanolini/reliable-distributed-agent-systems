"""Messages, signatures and the retrying client. The mechanics are Stage 7's.

Every participant (worker, checker, sender) holds a secret shared only with the
service it calls; a request is signed with HMAC-SHA256 over (participant, timestamp,
body) and refused outside a replay window. A registry maps each participant to its
secret and its role; the service decides what a role may ask for, so a worker cannot
deliver mail or judge drafts even if it tries.

A request that times out may or may not have been processed, so the client retries
with exponential backoff and jitter, resending the same body: the same request id,
the same token, the same draft. That is what lets the server recognise a repeat.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
import urllib.error
import urllib.request

from pydantic import BaseModel, Field

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


class Verdict(Renew):
    draft_hash: str
    approve: bool
    reason: str = Field(max_length=200)


class Sent(Msg):
    task: str


class Deliver(Msg):
    key: str
    to: str
    subject: str
    text: str = Field(max_length=4000)


def sign(secret: str, who: str, timestamp: str, body: bytes) -> str:
    msg = who.encode() + b"\n" + timestamp.encode() + b"\n" + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


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
    if not hmac.compare_digest(sign(registry[who]["secret"], who, timestamp, body), signature):
        return "bad signature"
    return None


class RPCError(Exception):
    def __init__(self, status: int, payload: dict):
        super().__init__(f"{status}: {payload}")
        self.status, self.payload = status, payload


class Client:
    def __init__(self, base_url: str, who: str, secret: str, timeout_s: float = 1.0,
                 attempts: int = 8, base_backoff_s: float = 0.05, max_backoff_s: float = 1.0):
        self.base_url, self.who, self.secret = base_url, who, secret
        self.timeout_s, self.attempts = timeout_s, attempts
        self.base_backoff_s, self.max_backoff_s = base_backoff_s, max_backoff_s
        self.retries = 0

    def call(self, path: str, message: BaseModel) -> dict:
        body = json.dumps(message.model_dump()).encode()       # fixed once: retries resend it verbatim
        for attempt in range(self.attempts):
            ts = f"{time.time():.3f}"
            req = urllib.request.Request(
                self.base_url + path, data=body, method="POST",
                headers={"Content-Type": "application/json", "X-Who": self.who, "X-Timestamp": ts,
                         "X-Signature": sign(self.secret, self.who, ts, body)})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:                  # the server answered: no retry
                raise RPCError(e.code, json.loads(e.read() or b"{}")) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == self.attempts - 1:
                    raise
                self.retries += 1
                pause = min(self.max_backoff_s, self.base_backoff_s * 2 ** attempt)
                time.sleep(pause * random.uniform(0.5, 1.5))
        raise AssertionError("unreachable")


def get_json(url: str, timeout_s: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            return json.loads(r.read())
    except OSError:
        return None
