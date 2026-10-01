"""The wire protocol between workers and the coordinator.

Messages are JSON objects validated against Pydantic models on arrival, so a
malformed message is rejected at the border instead of reaching the coordinator's
state. Every message carries a protocol version; a receiver refuses versions it
does not speak rather than guessing.

Worker identity: each worker holds a secret shared only with the coordinator. A
request is signed with HMAC-SHA256 over (worker id, timestamp, body). The
coordinator recomputes the signature with its copy of that worker's secret, so a
request cannot be forged or altered without the secret, and a signature older than
the replay window is refused. (Within the window a captured request could be
replayed; a per-request nonce would close that. Transport encryption, TLS, is out
of scope on a single host.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from pydantic import BaseModel, Field

VERSION = 1
REPLAY_WINDOW_S = 30


class Heartbeat(BaseModel):
    v: int
    worker: str


class Claim(BaseModel):
    v: int
    worker: str


class Complete(BaseModel):
    v: int
    worker: str
    job: str
    result: str = Field(min_length=1, max_length=256)


MESSAGES = {"/heartbeat": Heartbeat, "/claim": Claim, "/complete": Complete}


def sign(secret: str, worker: str, timestamp: str, body: bytes) -> str:
    msg = worker.encode() + b"\n" + timestamp.encode() + b"\n" + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def headers(secret: str, worker: str, body: bytes) -> dict[str, str]:
    ts = f"{time.time():.3f}"
    return {"Content-Type": "application/json", "X-Worker": worker, "X-Timestamp": ts,
            "X-Signature": sign(secret, worker, ts, body)}


def verify(secrets: dict[str, str], worker: str, timestamp: str, signature: str,
           body: bytes, now: float | None = None) -> str | None:
    """Return None if the request is authentic, else the reason it is not."""
    if worker not in secrets:
        return "unknown worker"
    try:
        age = (now or time.time()) - float(timestamp)
    except ValueError:
        return "malformed timestamp"
    if abs(age) > REPLAY_WINDOW_S:
        return "timestamp outside the replay window"
    expected = sign(secrets[worker], worker, timestamp, body)
    if not hmac.compare_digest(expected, signature):      # constant time: no timing leak
        return "bad signature"
    return None


def encode(message: BaseModel) -> bytes:
    return json.dumps(message.model_dump()).encode()
