"""The process boundary. Run: python -m unittest -v test_sandbox"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from client import ToolClient, ToolTimeout


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        (self.root / "a.txt").write_text("hello")
        self.egress = Path(self._tmp.name) / "egress.log"
        self.c = ToolClient(self.root, self.egress, timeout_s=0.5, test_tools=True)

    def tearDown(self):
        self.c.close()
        self._tmp.cleanup()

    def test_discovery_and_call(self):
        names = {t["name"] for t in self.c.list_tools()}
        self.assertTrue({"read_file", "list_dir", "search", "fetch_url"} <= names)
        self.assertEqual(self.c.call("read_file", {"path": "a.txt"}), ("hello", False))

    def test_tool_errors_are_results_not_crashes(self):
        text, is_error = self.c.call("read_file", {"path": "missing.txt"})
        self.assertTrue(is_error)
        self.assertIn("FileNotFoundError", text)

    def test_server_inherits_no_secrets(self):
        os.environ["SENTINEL_API_KEY"] = "sk-should-not-leak"
        try:
            names, _ = self.c.call("env_names", {})
        finally:
            del os.environ["SENTINEL_API_KEY"]
        self.assertNotIn("SENTINEL_API_KEY", names)
        self.assertNotIn("ANTHROPIC_API_KEY", names)
        self.assertNotIn("HOME", names)

    def test_overrunning_tool_is_killed_not_abandoned(self):
        self.c.list_tools()
        pid = self.c.proc.pid
        t0 = time.perf_counter()
        with self.assertRaises(ToolTimeout):
            self.c.call("sleep", {"seconds": 30})
        self.assertLess(time.perf_counter() - t0, 2.0)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)                          # the process no longer exists
        self.assertEqual(self.c.call("read_file", {"path": "a.txt"}), ("hello", False))
        self.assertEqual(self.c.spawned, 2)          # a fresh process served the next call

    def test_network_is_simulated_and_observable(self):
        self.c.call("fetch_url", {"url": "https://example.org/x"})
        self.assertEqual(self.egress.read_text(), "https://example.org/x\n")


if __name__ == "__main__":
    unittest.main()
