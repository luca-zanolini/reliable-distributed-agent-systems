"""Content addressing. Run: python -m unittest -v test_repo"""

import unittest

from repo import Repo, version_id


class ContentAddressing(unittest.TestCase):
    def test_same_contents_same_version(self):
        self.assertEqual(version_id({"a": "1", "b": "2"}), version_id({"b": "2", "a": "1"}))

    def test_any_change_is_a_new_version(self):
        self.assertNotEqual(version_id({"a": "1"}), version_id({"a": "1 "}))

    def test_write_moves_head_and_keeps_history(self):
        r = Repo({"lib.rs": "v0"})
        h0 = r.head
        h1 = r.write("lib.rs", "v1", author="impl-1")
        self.assertEqual(r.head, h1)
        self.assertEqual(r.read("lib.rs", h0), "v0")          # old versions stay readable
        self.assertEqual([c.version for c in r.log], [h0, h1])

    def test_reverting_contents_returns_to_the_same_version(self):
        r = Repo({"lib.rs": "v0"})
        h0 = r.head
        r.write("lib.rs", "v1", author="x")
        self.assertEqual(r.write("lib.rs", "v0", author="x"), h0)

    def test_naive_write_loses_concurrent_changes(self):
        # Two writers read the same version and each change a different line.
        r = Repo({"lib.rs": "A=0\nB=0\n"})
        base = r.read("lib.rs")
        r.write("lib.rs", base.replace("A=0", "A=1"), author="impl-1")
        r.write("lib.rs", base.replace("B=0", "B=1"), author="impl-2")   # built on the old read
        self.assertEqual(r.read("lib.rs"), "A=0\nB=1\n")                 # impl-1's change is gone


if __name__ == "__main__":
    unittest.main()
