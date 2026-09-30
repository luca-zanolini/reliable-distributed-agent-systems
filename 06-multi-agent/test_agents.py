"""Scheduler determinism and the serial baseline. Run: python -m unittest -v test_agents"""

import unittest

from agents import coordinator, implementer, planner, reviewer, run, tester
from board import Board
from repo import Repo
from software import INITIAL, run_tests


def team(board):
    return {"planner": planner(board), "impl-1": implementer("impl-1", board),
            "impl-2": implementer("impl-2", board), "tester": tester(board),
            "reviewer": reviewer(board), "coordinator": coordinator(board)}

SERIAL = ["planner*", "impl-1*", "impl-2*", "tester*", "reviewer*", "coordinator*"]


class Serial(unittest.TestCase):
    def test_serial_execution_is_correct(self):
        from scenarios import CORRECT                     # concurrent, but conflict-free
        board = Board(Repo(INITIAL))
        run(team(board), CORRECT)
        head = board.repo.head
        self.assertEqual(board.accepted, head)
        self.assertEqual(run_tests(board.repo.files(head))[1], [])            # accepted version passes
        self.assertEqual([r["version"] for r in board.reports], [head])       # evidence is about it
        self.assertEqual([r["version"] for r in board.reviews], [head])
        self.assertEqual(board.repo.read("CHANGELOG.md").count("fixed by"), 2)
        self.assertEqual({t.id: t.owner for t in board.tasks.values()}, {"T1": "impl-1", "T2": "impl-2"})

    def test_schedules_are_reproducible(self):
        def once(schedule):
            board = Board(Repo(INITIAL))
            return run(team(board), schedule), board.repo.head
        self.assertEqual(once(SERIAL), once(SERIAL))

    def test_a_lone_implementer_takes_every_open_task(self):
        board = Board(Repo(INITIAL))
        run(team(board), SERIAL)
        self.assertEqual({t.id: t.owner for t in board.tasks.values()}, {"T1": "impl-1", "T2": "impl-1"})
        self.assertEqual(board.accepted, board.repo.head)

    def test_one_step_at_a_time(self):
        board = Board(Repo(INITIAL))
        agents = team(board)
        self.assertEqual(len(run(agents, ["planner"])), 1)                    # one action, then paused
        self.assertEqual(list(board.tasks), ["T1"])


if __name__ == "__main__":
    unittest.main()
