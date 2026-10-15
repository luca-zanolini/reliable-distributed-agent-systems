"""Digital signatures (Ed25519), standard library only.

Stage 9 authenticated every request with an HMAC: a seal made and checked with the same
secret. Whoever can check a participant's seal can also forge it, and the front-ends held
every participant's secret. A signature separates the two powers: the private key makes a
seal, the public key only checks it. Anyone may hold the public key; only its owner can
sign. That is what lets a seal travel: a checker's verdict, signed once, can be checked
later by the mail service without the mail service, the ledger or the sender being able
to produce one.

This is the reference algorithm of RFC 8032 (section 6), in extended coordinates, with
the RFC's test vectors in test_sig.py. It is slow (a few milliseconds per operation) and
not hardened against timing attacks; both are irrelevant at the scale of this lab, and a
production system would use a vetted library.

Messages are signed in a canonical encoding (canon), so that the same document always
yields the same bytes wherever it is re-encoded.
"""

from __future__ import annotations

import hashlib
import json
import os

P = 2 ** 255 - 19
Q = 2 ** 252 + 27742317777372353535851937790883648493       # order of the base point
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _sha512_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _add(a, b):
    """Point addition in extended coordinates (X, Y, Z, T)."""
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % P
    B = (y1 + x1) * (y2 + x2) % P
    C = 2 * t1 * t2 * D % P
    Dd = 2 * z1 * z2 % P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def _mul(s: int, point):
    result = (0, 1, 1, 0)                                      # the neutral element
    while s > 0:
        if s & 1:
            result = _add(result, point)
        point = _add(point, point)
        s >>= 1
    return result


def _equal(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _recover_x(y: int, sign: int):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
BASE = (_GX, _GY, 1, _GX * _GY % P)


def _compress(point) -> bytes:
    zinv = pow(point[2], P - 2, P)
    x, y = point[0] * zinv % P, point[1] * zinv % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(data: bytes):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _expand(secret: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


# --- the interface: keys and signatures as hex strings ---------------------------------
def public_key(secret_hex: str) -> str:
    a, _ = _expand(bytes.fromhex(secret_hex))
    return _compress(_mul(a, BASE)).hex()


def keypair() -> tuple[str, str]:
    """A fresh (private, public) key pair. The private key never needs to leave its owner."""
    secret = os.urandom(32).hex()
    return secret, public_key(secret)


def sign_bytes(secret_hex: str, message: bytes) -> str:
    secret = bytes.fromhex(secret_hex)
    a, prefix = _expand(secret)
    pub = _compress(_mul(a, BASE))
    r = _sha512_int(prefix + message) % Q
    R = _compress(_mul(r, BASE))
    h = _sha512_int(R + pub + message) % Q
    s = (r + h * a) % Q
    return (R + s.to_bytes(32, "little")).hex()


def verify_bytes(public_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        pub, sig = bytes.fromhex(public_hex), bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        return False
    if len(pub) != 32 or len(sig) != 64:
        return False
    A, R = _decompress(pub), _decompress(sig[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= Q:
        return False
    h = _sha512_int(sig[:32] + pub + message) % Q
    return _equal(_mul(s, BASE), _add(R, _mul(h, A)))


def canon(doc) -> bytes:
    """The one byte encoding of a document that everybody signs and checks."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(secret_hex: str, doc) -> str:
    return sign_bytes(secret_hex, canon(doc))


def verify(public_hex: str, doc, signature_hex: str) -> bool:
    return verify_bytes(public_hex, canon(doc), signature_hex)


def digest(data: str | bytes) -> str:
    """SHA-256, in full: a short hash would let someone search for a second text with the
    same fingerprint, and the fingerprint is what the signatures are about."""
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()
