import logging
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import herdr_remote as hr  # noqa: E402
from test_herdr_remote import make_app, st  # noqa: E402

hr.log.addHandler(logging.NullHandler())
hr.log.propagate = False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class BlockedSinceTest(unittest.TestCase):
    def test_keeps_first_time_and_resets(self):
        c = Clock()
        b = hr.BlockedSince(wall=c)
        self.assertEqual(b.update({"t1": True, "t2": False}), {"t1": 1000000, "t2": None})
        c.t = 1060.0
        self.assertEqual(b.update({"t1": True})["t1"], 1000000)  # 계속 막힘이면 처음 시각 유지
        self.assertEqual(b.update({"t1": False})["t1"], None)
        c.t = 1100.0
        self.assertEqual(b.update({"t1": True})["t1"], 1100000)  # 풀렸다 다시 막히면 새로 센다

    def test_gone_terminal_forgotten(self):
        c = Clock()
        b = hr.BlockedSince(wall=c)
        b.update({"t1": True})
        b.update({})
        c.t = 2000.0
        self.assertEqual(b.update({"t1": True})["t1"], 2000000)

    def test_api_terminals_field(self):
        app, hc, _ = make_app()
        hc.add("t1", st("blocked", "x"))
        hc.add("t2", st("working"))
        res = {t["id"]: t for t in app.api_terminals()["terminals"]}
        self.assertIsInstance(res["t1"]["blocked_since"], int)
        self.assertIsNone(res["t2"]["blocked_since"])


if __name__ == "__main__":
    unittest.main()
