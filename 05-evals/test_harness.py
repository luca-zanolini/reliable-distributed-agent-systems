"""The harness, driven by scripted agents with known behaviour. Run: python -m unittest -v test_harness"""

import json
import tempfile
import unittest
from pathlib import Path

from harness import Config, run_suite  # first: sets the import path
from llm import FakeProvider, tool_call
from tasks import REPO, SUITE

FIXED = REPO["src/fencing.rs"].replace("} else {\n            Decision::Accepted",
                                       "} else {\n            Decision::Rejected")

GOOD = {
    "diagnose": [tool_call("read_file", path="test-output.log"),
                 "fencing::stale_writer_rejected fails; the bug is in src/fencing.rs."],
    "count": [tool_call("read_file", path="test-output.log"), "ran=5 failed=1"],
    "locate": [tool_call("search", text="fn grant"), "src/leases.rs:3"],
    "compute": [tool_call("read_file", path="metrics/lease-durations-ms.txt"),
                tool_call("calculate", expression="(1500+2250+4000+5000)/1000"), "12.75"],
    "fix": [tool_call("write_file", path="src/fencing.rs", content=FIXED),
            "The stale branch now returns Decision::Rejected."],
}

CLAIMS_ONLY = {**GOOD, "fix": [tool_call("read_file", path="src/fencing.rs"),
                               "Fixed: the stale branch now returns Decision::Rejected."]}
NEVER_STOPS = {t: [tool_call("calculate", expression=f"{i}+1") for i in range(50)] for t in GOOD}


def scripted(scripts, cost=0.001):
    return lambda task, k: FakeProvider(list(scripts[task.id]), cost_usd_per_call=cost)


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def records(self, path):
        return [json.loads(l) for l in path.read_text().splitlines()]

    def test_good_agent_passes_everything(self):
        recs = self.records(run_suite(Config("good", "fake"), scripted(GOOD), 2, self.out))
        self.assertEqual(len(recs), 2 * len(SUITE))
        self.assertEqual({r["category"] for r in recs}, {"passed"})

    def test_records_carry_the_required_fields(self):
        rec = self.records(run_suite(Config("good", "fake"), scripted(GOOD), 1, self.out))[0]
        for field in ["run_id", "task_id", "config", "config_id", "model", "system_prompt_sha", "suite_id",
                      "status", "validator_passed", "validator_reason", "category", "latency_s", "cost_usd",
                      "model_calls", "tool_calls", "artifacts", "provenance"]:
            self.assertIn(field, rec)
        self.assertTrue((self.out / "traces" / f"{rec['run_id']}.jsonl").exists())

    def test_claim_without_evidence_is_caught(self):
        recs = self.records(run_suite(Config("liar", "fake"), scripted(CLAIMS_ONLY), 1, self.out))
        fix = next(r for r in recs if r["task_id"] == "fix")
        self.assertEqual(fix["status"], "answered")                 # the agent says it is done
        self.assertEqual(fix["category"], "claim_without_evidence")  # the file says otherwise

    def test_artifact_hashes_show_the_change(self):
        good = self.records(run_suite(Config("good", "fake"), scripted(GOOD), 1, self.out))
        liar = self.records(run_suite(Config("liar", "fake"), scripted(CLAIMS_ONLY), 1, self.out))
        h = lambda recs: next(r for r in recs if r["task_id"] == "fix")["artifacts"]["src/fencing.rs"]
        self.assertNotEqual(h(good), h(liar))

    def test_budget_exhaustion_is_its_own_category(self):
        recs = self.records(run_suite(Config("loop", "fake", max_steps=4), scripted(NEVER_STOPS), 1, self.out))
        self.assertEqual({r["category"] for r in recs}, {"step_budget"})

    def test_configurations_are_distinguishable(self):
        a, b = Config("a", "m1"), Config("a", "m1", system="other prompt")
        self.assertNotEqual(a.id(), b.id())


if __name__ == "__main__":
    unittest.main()
