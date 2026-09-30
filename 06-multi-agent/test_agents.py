"""Scheduler determinism and the serial baseline. Run: python -m unittest -v test_agents"""

import unittest

from agents import sign_off, implementer, planner, reviewer, run, tester
from coordinator import Coordinator
from repo import Repo
from software import INITIAL, run_tests


def team(coord):
    return {"planner": planner(coord), "impl-1": implementer("impl-1", coord),
            "impl-2": implementer("impl-2", coord), "tester": tester(coord),
            "reviewer": reviewer(coord), "coordinator": sign_off(coord)}

SERIAL = ["planner*", "impl-1*", "impl-2*", "tester*", "reviewer*", "coordinator*"]


class Serial(unittest.TestCase):
    def test_serial_execution_is_correct(self):
        from scenarios import CORRECT                     # concurrent, but conflict-free
        coord = Coordinator(Repo(INITIAL))
        run(team(coord), CORRECT)
        head = coord.repo.head
        self.assertEqual(coord.accepted, head)
        self.assertEqual(run_tests(coord.repo.files(head))[1], [])            # accepted version passes
        self.assertEqual([r["version"] for r in coord.reports], [head])       # evidence is about it
        self.assertEqual([r["version"] for r in coord.reviews], [head])
        self.assertEqual(coord.repo.read("CHANGELOG.md").count("fixed by"), 2)
        self.assertEqual({t.id: t.owner for t in coord.tasks.values()}, {"T1": "impl-1", "T2": "impl-2"})

    def test_schedules_are_reproducible(self):
        def once(schedule):
            coord = Coordinator(Repo(INITIAL))
            return run(team(coord), schedule), coord.repo.head
        self.assertEqual(once(SERIAL), once(SERIAL))

    def test_a_lone_implementer_takes_every_open_task(self):
        coord = Coordinator(Repo(INITIAL))
        run(team(coord), SERIAL)
        self.assertEqual({t.id: t.owner for t in coord.tasks.values()}, {"T1": "impl-1", "T2": "impl-1"})
        self.assertEqual(coord.accepted, coord.repo.head)

    def test_one_step_at_a_time(self):
        coord = Coordinator(Repo(INITIAL))
        agents = team(coord)
        self.assertEqual(len(run(agents, ["planner"])), 1)                    # one action, then paused
        self.assertEqual(list(coord.tasks), ["T1"])


if __name__ == "__main__":
    unittest.main()
