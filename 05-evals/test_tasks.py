"""Validators judged against known-good and known-bad outcomes. Run: python -m unittest -v test_tasks"""

import tempfile
import unittest
from pathlib import Path

from tasks import REPO, SUITE

BY_ID = {t.id: t for t in SUITE}


def workspace(tmp, overrides=None):
    for rel, text in {**REPO, **(overrides or {})}.items():
        (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
        (Path(tmp) / rel).write_text(text)
    return Path(tmp)


class Validators(unittest.TestCase):
    def check(self, task_id, answer, overrides=None):
        with tempfile.TemporaryDirectory() as tmp:
            return BY_ID[task_id].validator(answer, workspace(tmp, overrides))[0]

    def test_diagnose(self):
        self.assertTrue(self.check("diagnose", "fencing::stale_writer_rejected fails; see src/fencing.rs."))
        self.assertFalse(self.check("diagnose", "quorum::intersection fails."))

    def test_answer_format_is_enforced(self):
        self.assertTrue(self.check("count", "ran=5 failed=1"))
        self.assertFalse(self.check("count", "5 tests ran and 1 failed"))       # right facts, wrong format
        self.assertTrue(self.check("locate", "src/leases.rs:3"))
        self.assertFalse(self.check("locate", "src/leases.rs, line 3"))
        self.assertTrue(self.check("compute", "12.75"))
        self.assertFalse(self.check("compute", "12750"))

    def test_format_violation_is_distinguished_from_wrong_values(self):
        # Observed live (claude-opus-4-8): right values, preceded by a sentence.
        with tempfile.TemporaryDirectory() as tmp:
            ok, reason = BY_ID["count"].validator(
                'The log shows "running 5 tests" with 4 passed and 1 failed.\n\nran=5 failed=1', Path(tmp))
            self.assertFalse(ok)
            self.assertTrue(reason.startswith("format:"))
            ok, reason = BY_ID["count"].validator("ran=5 failed=2", Path(tmp))
            self.assertFalse(reason.startswith("format:"))

    def test_fix_is_judged_on_the_file_not_the_claim(self):
        fixed = REPO["src/fencing.rs"].replace(
            "} else {\n            Decision::Accepted", "} else {\n            Decision::Rejected")
        self.assertTrue(self.check("fix", "Fixed.", {"src/fencing.rs": fixed}))
        self.assertFalse(self.check("fix", "Fixed: the stale branch now returns Rejected."))  # claim only

    def test_fix_that_breaks_the_other_branch_fails(self):
        wrong = REPO["src/fencing.rs"].replace("Decision::Accepted", "Decision::Rejected")
        self.assertFalse(self.check("fix", "Fixed.", {"src/fencing.rs": wrong}))

    def test_keyword_validator_false_positive_is_real(self):
        # Documented limitation: the right words in a wrong statement pass.
        self.assertTrue(self.check("diagnose",
                                   "Not stale_writer_rejected and not src/fencing.rs; it is the quorum test."))


if __name__ == "__main__":
    unittest.main()
