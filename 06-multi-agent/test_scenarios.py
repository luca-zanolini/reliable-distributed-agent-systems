"""Every naive failure reproduces on demand; the baseline does not fail.
Run: python -m unittest -v test_scenarios"""

import unittest

from scenarios import SCENARIOS, diagnose, execute

BY_KEY = {s.key: s for s in SCENARIOS}

EXPECTED = {       # a fragment of each finding the record must show
    "a": ["T1 had two owners at once", "T1 was carried out 2 times"],
    "b": ["without a review of it"],
    "c": ["without a passing test report for it", "the accepted version fails ['grant_increments']"],
    "d": ["T1 was carried out 2 times"],
    "e": ["T1 is marked done, but its fix is not in the head", "nothing accepted"],
    "f": ["forgot task states", "T1 was carried out 2 times"],
}


class Reproduction(unittest.TestCase):
    def test_baseline_is_clean(self):
        _, board, crashed = execute(None)
        self.assertEqual(diagnose(board, crashed), [])

    def test_each_failure_reproduces(self):
        for key, fragments in EXPECTED.items():
            with self.subTest(scenario=key):
                _, board, crashed = execute(BY_KEY[key])
                found = " | ".join(diagnose(board, crashed))
                for frag in fragments:
                    self.assertIn(frag, found)

    def test_reproduction_is_deterministic(self):
        for s in SCENARIOS:
            self.assertEqual(execute(s)[0], execute(s)[0])

    def test_naive_board_accepts_silently_where_it_should_not(self):
        # a, b, c, d and f end with an acceptance: the naive coordinator saw nothing wrong.
        for key in "abcdf":
            with self.subTest(scenario=key):
                self.assertIsNotNone(execute(BY_KEY[key])[1].accepted)


if __name__ == "__main__":
    unittest.main()
