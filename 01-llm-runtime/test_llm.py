"""Adapter contract tests. Offline and free: every test uses FakeProvider.

Run: python -m unittest -v
"""

import unittest

from pydantic import BaseModel

from llm import (
    STOP_END, FakeProvider, OutputError, TransientError, _ANTHROPIC_STOPS, cost_usd,
)


class Report(BaseModel):
    passed: bool
    tests_run: int


class FreeForm(unittest.TestCase):
    def test_returns_scripted_text_with_receipt(self):
        p = FakeProvider(["all five tests passed"])
        c = p.complete("summarize the log")
        self.assertEqual(c.text, "all five tests passed")
        self.assertIsNone(c.parsed)
        self.assertEqual(c.stop, STOP_END)
        self.assertEqual((c.input_tokens, c.output_tokens), (3, 4))
        self.assertEqual(p.prompts, ["summarize the log"])

    def test_truncation_is_an_error_not_a_short_answer(self):
        p = FakeProvider(["one two three four five"])
        with self.assertRaises(OutputError) as ctx:
            p.complete("summarize", max_tokens=3)
        self.assertEqual(ctx.exception.completion.output_tokens, 3)   # spend still recorded


class Structured(unittest.TestCase):
    def test_valid_json_becomes_typed_object(self):
        p = FakeProvider(['{"passed": false, "tests_run": 5}'])
        c = p.complete("extract", schema=Report)
        self.assertIsInstance(c.parsed, Report)
        self.assertEqual(c.parsed.tests_run, 5)
        self.assertIsNone(c.text)

    def test_schema_violation_is_an_error(self):
        p = FakeProvider(['{"passed": "maybe", "tests_run": 5}'])
        with self.assertRaises(OutputError):
            p.complete("extract", schema=Report)

    def test_prose_where_json_was_required_is_an_error(self):
        p = FakeProvider(["The tests mostly passed."])
        with self.assertRaises(OutputError):
            p.complete("extract", schema=Report)


class Failures(unittest.TestCase):
    def test_scripted_transport_failure_propagates(self):
        p = FakeProvider([TransientError("503 after retries"), "recovered"])
        with self.assertRaises(TransientError):
            p.complete("first attempt")
        self.assertEqual(p.complete("second attempt").text, "recovered")


class Accounting(unittest.TestCase):
    def test_cost_matches_a_measured_call(self):
        # Observed on claude-opus-4-8: 154 input, 300 output tokens -> $0.008270.
        self.assertAlmostEqual(cost_usd("claude-opus-4-8", 154, 300), 0.008270)

    def test_output_tokens_dominate(self):
        self.assertEqual(cost_usd("claude-opus-4-8", 0, 1) / cost_usd("claude-opus-4-8", 1, 0), 5)

    def test_anthropic_truncation_maps_to_neutral_vocabulary(self):
        self.assertEqual(_ANTHROPIC_STOPS["max_tokens"], "truncated")
        self.assertEqual(_ANTHROPIC_STOPS["refusal"], "refused")


if __name__ == "__main__":
    unittest.main()
