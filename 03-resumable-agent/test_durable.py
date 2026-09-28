"""Crash and recovery suite. Offline and free.

A crash is simulated by raising an exception that nothing catches (like a kill,
it skips every remaining step); recovery is a brand-new agent object on the same
journal, so only what reached the journal survives. crash_demo.py repeats the
central scenarios with real process kills.

Run: python -m unittest -v
"""

import json
import tempfile
import unittest
from pathlib import Path

import crash_demo
from durable_agent import DurableAgent
from effects import APPEND_NOTE, SEND_MESSAGE
from journal import CorruptJournal, Journal
from llm import ToolCall, TransientError
from scripted import ScriptedModel
from tools import READ_ONLY, WRITE_FILE, Workspace

OBJ = "objective"


class Crash(BaseException):
    pass


def crash_at(point, tool=None):
    def hook(p, call=None):
        if p == point and (tool is None or (call is not None and call.name == tool)):
            raise Crash(p)
    return hook


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "log.txt").write_text("FAILED\n")
        self.journal = self.ws / ".journal.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def agent(self, script, hook=None, cost=0.0, tools=None):
        self.model = ScriptedModel(script, cost)
        tools = tools or READ_ONLY + [WRITE_FILE, SEND_MESSAGE, APPEND_NOTE]
        return DurableAgent(self.model, Workspace(self.ws), tools, self.journal, crash_hook=hook)

    def crash_then_resume(self, script, point, tool=None, cost=0.0):
        with self.assertRaises(Crash):
            self.agent(script, crash_at(point, tool), cost).run(OBJ)
        return self.agent(script, cost=cost).run(OBJ)

    def records(self, kind):
        return [r for r in Journal(self.journal).load() if r["type"] == kind]

    def lines(self, name):
        p = self.ws / name
        return p.read_text().splitlines() if p.exists() else []


SEND = [ToolCall("c1", "send_message", {"to": "on-call", "text": "x"}), "done"]
NOTE = [ToolCall("c1", "append_note", {"text": "x"}), "done"]


class NormalRun(Base):
    def test_journal_records_every_step_in_order(self):
        r = self.agent([ToolCall("c1", "read_file", {"path": "log.txt"}), "done"]).run(OBJ)
        self.assertEqual(r.status, "answered")
        kinds = [x["type"] for x in Journal(self.journal).load()]
        self.assertEqual(kinds, ["run_started", "model_turn", "tool_intent", "tool_result",
                                 "model_turn", "run_finished"])

    def test_finished_run_is_not_rerun(self):
        self.agent(["done"]).run(OBJ)
        r = self.agent(["SHOULD NOT BE ASKED"]).run(OBJ)
        self.assertEqual((r.status, r.answer), ("answered", "done"))
        self.assertEqual(self.model.calls, 0)

    def test_journal_of_another_objective_is_refused(self):
        self.agent(["done"]).run(OBJ)
        with self.assertRaises(ValueError):
            self.agent(["done"]).run("a different objective")


class CrashPoints(Base):
    def test_crash_after_intent_before_execution_keyed_runs_once(self):
        r = self.crash_then_resume(SEND, "after_intent", "send_message")
        self.assertEqual(len(self.lines("outbox.jsonl")), 1)
        self.assertEqual(r.status, "answered")

    def test_crash_after_execution_keyed_is_deduplicated(self):
        # The message went out, the result was never recorded: re-run with the same key.
        self.crash_then_resume(SEND, "after_execute", "send_message")
        self.assertEqual(len(self.lines("outbox.jsonl")), 1)
        (res,) = self.records("tool_result")
        self.assertIn("already delivered", res["content"])

    def test_crash_after_execution_unsafe_is_not_repeated(self):
        r = self.crash_then_resume(NOTE, "after_execute", "append_note")
        self.assertEqual(self.lines("notes.log"), ["x"])                 # exactly once
        (res,) = self.records("tool_result")
        self.assertTrue(res["is_error"])
        self.assertIn("outcome unknown", res["content"])                 # the model is told
        self.assertEqual(r.status, "answered")

    def test_unsafe_intent_is_conservative_even_if_the_action_never_ran(self):
        # Crash between intent and execution: the note was never written, but the
        # runtime cannot tell this case from the previous one, so it still refuses.
        self.crash_then_resume(NOTE, "after_intent", "append_note")
        self.assertEqual(self.lines("notes.log"), [])                    # at most once
        self.assertIn("outcome unknown", self.records("tool_result")[0]["content"])

    def test_crash_after_execution_idempotent_write_is_rerun(self):
        script = [ToolCall("c1", "write_file", {"path": "cfg", "content": "v2"}), "done"]
        self.crash_then_resume(script, "after_execute", "write_file")
        self.assertEqual((self.ws / "cfg").read_text(), "v2")

    def test_completed_calls_are_not_rerun_after_a_crash_later_in_the_turn(self):
        script = [[ToolCall("c1", "send_message", {"to": "a", "text": "x"}),
                   ToolCall("c2", "append_note", {"text": "y"})], "done"]
        self.crash_then_resume(script, "after_intent", "append_note")
        self.assertEqual(len(self.lines("outbox.jsonl")), 1)             # c1 reused, not re-sent
        self.assertEqual(len(self.records("tool_intent")), 2)

    def test_recorded_model_turns_are_not_re_requested(self):
        script = [ToolCall("c1", "read_file", {"path": "log.txt"}),
                  ToolCall("c2", "read_file", {"path": "log.txt"}), "done"]
        self.crash_then_resume(script, "after_model_turn")
        self.assertEqual(self.model.calls, 2)        # resumed process asked only for turns 2 and 3...
        self.assertEqual(len(self.records("model_turn")), 3)

    def test_crash_after_final_answer_recorded(self):
        with self.assertRaises(Crash):
            self.agent(["done"], crash_at("after_model_turn")).run(OBJ)
        r = self.agent(["SHOULD NOT BE ASKED"]).run(OBJ)
        self.assertEqual((r.status, r.answer, self.model.calls), ("answered", "done", 0))

    def test_model_call_in_flight_is_re_requested_and_its_cost_is_lost(self):
        # Known gap: a call answered but not yet journaled was billed, but the
        # journal never learned its cost. The restart pays for it again.
        r = self.crash_then_resume(["done"], "after_model_call", cost=0.01)
        self.assertEqual(r.status, "answered")
        self.assertAlmostEqual(r.cost_usd, 0.01)     # true spend was 0.02

    def test_refusals_are_replayed_not_re_decided(self):
        script = [ToolCall("c1", "read_file", {"path": "/etc/passwd"}),
                  ToolCall("c2", "read_file", {"path": "log.txt"}), "done"]
        self.crash_then_resume(script, "after_intent")
        (denied, ok) = self.records("tool_result")
        self.assertTrue(denied["content"].startswith("denied"))
        self.assertFalse(ok["is_error"])


class Budgets(Base):
    def test_spend_survives_restarts(self):
        script = [ToolCall(f"c{i}", "calculate", {"expression": f"{i}+1"}) for i in range(20)]
        for _ in range(3):                      # crash after every recorded turn, three times
            with self.assertRaises(Crash):
                self.agent(script, crash_at("after_model_turn"), cost=0.1).run(OBJ)
        agent = self.agent(script, cost=0.1)
        agent.budget = type(agent.budget)(max_cost_usd=0.45)
        r = agent.run(OBJ)
        self.assertEqual(r.status, "cost_budget")
        self.assertAlmostEqual(r.cost_usd, 0.5)  # 3 turns before restarts + 2 after, not 5 fresh

    def test_transient_failure_pauses_and_the_run_resumes_later(self):
        script = [ToolCall("c1", "read_file", {"path": "log.txt"}), TransientError("503"), "done"]
        r1 = self.agent(script).run(OBJ)
        self.assertEqual(r1.status, "paused")
        script[1] = "done"                      # the provider is back
        r2 = self.agent(script).run(OBJ)
        self.assertEqual((r2.status, r2.answer), ("answered", "done"))


class JournalFile(Base):
    def test_torn_final_line_is_discarded(self):
        self.agent(["done"]).run(OBJ)
        with open(self.journal, "a") as f:
            f.write('{"type": "model_tu')          # a write cut short by a crash
        records = Journal(self.journal).load()
        self.assertEqual(records[-1]["type"], "run_finished")
        self.assertTrue(self.journal.read_text().endswith("\n"))      # clean boundary restored

    def test_damage_before_the_end_is_corruption(self):
        self.journal.write_text('{"type": "run_started"}\nnot json\n{"type": "x"}\n')
        with self.assertRaises(CorruptJournal):
            Journal(self.journal).load()


class RealKill(unittest.TestCase):
    """The crash_demo experiment, asserted: a real os._exit mid-action."""

    def test_naive_restart_duplicates_every_effect(self):
        for scenario in ("keyed", "unsafe"):
            code, effects, _ = crash_demo.experiment("naive", scenario)
            self.assertEqual((code, effects), (137, 2))

    def test_durable_restart_performs_each_effect_once(self):
        for scenario in ("keyed", "unsafe"):
            code, effects, r = crash_demo.experiment("durable", scenario)
            self.assertEqual((code, effects, r.status), (137, 1, "answered"))


if __name__ == "__main__":
    unittest.main()
