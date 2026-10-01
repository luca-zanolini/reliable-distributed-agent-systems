"""Message validation and authentication. Run: python -m unittest -v test_protocol"""

import time
import unittest

import pydantic

from protocol import Complete, encode, headers, verify

SECRETS = {"w1": "secret-one", "w2": "secret-two"}


class Authentication(unittest.TestCase):
    def setUp(self):
        self.body = encode(Complete(v=1, worker="w1", job="J1", result="abc"))
        h = headers(SECRETS["w1"], "w1", self.body)
        self.ts, self.sig = h["X-Timestamp"], h["X-Signature"]

    def test_authentic_request_verifies(self):
        self.assertIsNone(verify(SECRETS, "w1", self.ts, self.sig, self.body))

    def test_altered_body_is_rejected(self):
        tampered = self.body.replace(b"abc", b"xyz")
        self.assertEqual(verify(SECRETS, "w1", self.ts, self.sig, tampered), "bad signature")

    def test_another_workers_identity_cannot_be_claimed(self):
        # w2 signs with its own secret but claims to be w1
        h = headers(SECRETS["w2"], "w1", self.body)
        self.assertEqual(verify(SECRETS, "w1", h["X-Timestamp"], h["X-Signature"], self.body), "bad signature")

    def test_unknown_worker(self):
        self.assertEqual(verify(SECRETS, "w9", self.ts, self.sig, self.body), "unknown worker")

    def test_old_signature_is_refused(self):
        self.assertEqual(verify(SECRETS, "w1", self.ts, self.sig, self.body, now=time.time() + 120),
                         "timestamp outside the replay window")


class Validation(unittest.TestCase):
    def test_malformed_message_is_rejected_at_the_border(self):
        with self.assertRaises(pydantic.ValidationError):
            Complete.model_validate({"v": 1, "worker": "w1", "job": "J1"})          # no result
        with self.assertRaises(pydantic.ValidationError):
            Complete.model_validate({"v": 1, "worker": "w1", "job": "J1", "result": ""})


if __name__ == "__main__":
    unittest.main()
