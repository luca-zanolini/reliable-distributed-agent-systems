"""Summaries and regression detection. Run: python -m unittest -v test_report"""

import unittest

from report import overall, regressions, summarize, table


def rec(task, cat, cost=0.01, suite="s1", config="c", model="m"):
    return {"task_id": task, "category": cat, "cost_usd": cost, "latency_s": 1.0, "tool_calls": 2,
            "suite_id": suite, "config": config, "model": model}


class Report(unittest.TestCase):
    def test_pass_rate_and_consistency(self):
        s = summarize([rec("t", "passed"), rec("t", "passed"), rec("t", "wrong_answer")])["t"]
        self.assertAlmostEqual(s["pass_rate"], 2 / 3)
        self.assertFalse(s["all_passed"])                   # passes sometimes is not reliable
        self.assertEqual(s["failures"], {"wrong_answer": 1})

    def test_regression_is_flagged(self):
        base = [rec("a", "passed"), rec("a", "passed"), rec("b", "passed")]
        cand = [rec("a", "passed"), rec("a", "wrong_answer"), rec("b", "passed")]
        self.assertEqual(regressions(cand, base), ["a: pass rate 100% -> 50%"])
        self.assertEqual(regressions(base, cand), [])       # an improvement is not a regression

    def test_different_suites_are_not_compared(self):
        with self.assertRaises(ValueError):
            regressions([rec("a", "passed", suite="s2")], [rec("a", "passed", suite="s1")])

    def test_table_and_totals(self):
        rs = [rec("a", "passed", 0.02), rec("a", "step_budget", 0.03)]
        self.assertAlmostEqual(overall(rs)["total_cost_usd"], 0.05)
        self.assertIn("step_budget×1", table(rs))


if __name__ == "__main__":
    unittest.main()
