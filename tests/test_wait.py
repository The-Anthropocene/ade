# id: tests.test_wait
# what: the watcher's invariant: every wait ends, met or by its bound, and a quiet lane reads as stalled
# cmd: python3 -m unittest tests/test_wait.py
# reads: tests/fixture_repo
# writes: temporary directories only
import os
import threading
import time

from ade import config, records, wait # noqa: F401  (imported so the graph records coverage)
from ade.verbs import wait as waitverb  # noqa: F401
from tests.helpers import FixtureCase


class WaitTest(FixtureCase):
    def test_every_wait_ends(self):
        self.install()
        self.assertEqual(self.ade("wait", "clean", "--bound", "5s")[0], 0)  # a fresh install is on trunk and clean
        began = time.monotonic()
        code, out = self.ade("wait", "exit", str(os.getpid()), "--bound", "1s", "--poll", "0.1")
        self.assertEqual(code, 4, out)  # this process outlives the wait, so only the bound can end it
        self.assertLess(time.monotonic() - began, 5)

        log = self.repo / ".ade/lanes/c1.log"
        log.parent.mkdir(parents=True)
        log.write_text("builder started\n")
        os.utime(log, (time.time() - 3600, time.time() - 3600))
        code, out = self.ade("wait", "journal", "c1", "--lane", "c1", "--bound", "5s", "--poll", "0.1")
        self.assertEqual(code, 5, out)  # an hour of silence is a stall, not a quiet lane

        # A stage wait ends on that stage's journal row written after it began; any stop row (wait, needs-judgement) is a non-zero exit.
        ctx = config.load(self.repo)
        for stage, outcome, want in (("build", "advance", 0), ("review", "needs-judgement", 3)):
            row = {"card": "c1", "stage": stage, "outcome": outcome, "note": "n", "next": ""}
            threading.Timer(0.5, records.append, (ctx, "journal", row)).start()
            code, out = self.ade("wait", "stage", "c1", stage, "--bound", "10s", "--poll", "0.1")
            self.assertEqual(code, want, out)
            self.assertIn(outcome, out)

        # A stop on an earlier stage ends a later stage's wait too (it would otherwise sit to its bound).
        row = {"card": "c1", "stage": "build", "outcome": "parked", "note": "n", "next": ""}
        threading.Timer(0.5, records.append, (ctx, "journal", row)).start()
        code, out = self.ade("wait", "stage", "c1", "land", "--bound", "10s", "--poll", "0.1")
        self.assertEqual(code, 3, out)
