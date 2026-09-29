"""Traces record what happened, in order. Run: python -m unittest -v test_tracing"""

import json
import tempfile
import unittest
from pathlib import Path

from tracing import Trace, TracedAgent, TracedProvider  # first: sets the import path
from llm import FakeProvider, tool_call
from tools import READ_ONLY, Workspace


class Tracing(unittest.TestCase):
    def test_spans_follow_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.txt").write_text("x")
            trace = Trace("run-1")
            model = TracedProvider(FakeProvider([
                [tool_call("read_file", path="a.txt"), tool_call("read_file", path="/etc/passwd")],
                "done"], cost_usd_per_call=0.01), trace)
            r = TracedAgent(model, Workspace(tmp), READ_ONLY, trace=trace).run("objective")

            self.assertEqual(r.status, "answered")
            self.assertEqual([(s["kind"], s.get("tool"), s.get("outcome")) for s in trace.spans], [
                ("model_call", None, None),
                ("tool_call", "read_file", "executed"),
                ("tool_call", "read_file", "denied"),
                ("model_call", None, None),
            ])
            self.assertTrue(all(s["run_id"] == "run-1" for s in trace.spans))
            self.assertEqual(trace.totals()["tool_calls_refused"], 1)
            self.assertAlmostEqual(sum(s.get("cost_usd", 0) for s in trace.spans), 0.02)

            out = Path(tmp) / "traces" / "run-1.jsonl"
            trace.write(out)
            self.assertEqual(len(out.read_text().splitlines()), 4)
            self.assertEqual(json.loads(out.read_text().splitlines()[0])["seq"], 0)

    def test_failed_model_call_is_traced_then_raised(self):
        trace = Trace("run-2")
        model = TracedProvider(FakeProvider([RuntimeError("down")]), trace)
        with self.assertRaises(RuntimeError):
            model.step([])
        self.assertEqual(trace.spans[0]["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
