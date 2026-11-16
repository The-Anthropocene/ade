# id: tests.test_session
# what: the sessions invariant: a started session outlives the call that started it, is listed live, stops, and a reused pid is not it
# cmd: python3 -m unittest tests/test_session.py
# reads: tests/fixture_repo
# writes: temporary directories only
import io
import json
import os
import time
from contextlib import redirect_stdout
from unittest import mock

from ade import config
from ade.verbs import session
from tests.helpers import FixtureCase

real_sleep = time.sleep  # kept apart: the follow test patches time.sleep


class SessionTest(FixtureCase):
    def listed(self, name):
        return next(line.split()[1] for line in self.ade("session", "list")[1].splitlines() if line.split()[0] == name)

    def test_session_runs_detached_until_stopped_and_attach_follow_shows_every_run_of_a_name(self):
        self.install()
        code, out = self.ade("session", "start", "nap", "--", "wait", "time", "10m")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.listed("nap"), "live")  # start returned; the verb is still running on its own
        self.assertEqual(self.ade("session", "start", "nap", "--", "wait", "time", "1s")[0], 2)  # one per name
        self.assertEqual(self.ade("session", "stop", "nap")[0], 0)
        for _ in range(50):
            if self.listed("nap") == "ended":
                break
            time.sleep(0.1)
        self.assertEqual(self.listed("nap"), "ended")

        # A pid handed out again to another process (here, this one) is not the session that once had it.
        ghost = {"type": "session", "at": "2026-01-01T00:00:00+00:00", "name": "ghost", "pid": os.getpid(),
                 "started": "not this process", "argv": ["wait", "time", "1s"], "log": ".ade/sessions/ghost.log"}
        with open(self.repo / ".ade/records.jsonl", "a") as f:
            f.write(json.dumps(ghost) + "\n")
        self.assertEqual(self.listed("ghost"), "ended")

        # Guards the contract: `attach --follow` prints a session's log, says it ended, then follows the next
        # session of the same name (an Orca tab reused by title) instead of exiting.
        log = self.repo / ".ade/sessions/rerun.log"

        def run_once(text):
            self.assertEqual(self.ade("session", "start", "rerun", "--", "wait", "time", "1s")[0], 0)
            # Write the marker only once the session has ended: on Windows the child's inherited log handle
            # writes at its own offset and would overwrite text appended while it is still running.
            for _ in range(100):
                if self.listed("rerun") == "ended":
                    break
                real_sleep(0.1)
            with open(log, "a") as f:
                f.write(text)

        run_once("first-run-log\n")
        ctx, polls, out = config.load(self.repo), [0], io.StringIO()
        s = session._get(ctx, "rerun")

        def sleeper(_):
            polls[0] += 1
            if polls[0] == 2:
                run_once("second-run-log\n")
            if polls[0] > 6:
                raise KeyboardInterrupt

        with redirect_stdout(out), mock.patch.object(session.time, "sleep", sleeper):
            self.assertEqual(session._attach(ctx, s, 0, True), 0)
        text = out.getvalue()
        self.assertLess(text.index("first-run-log"), text.index("ended"))
        self.assertIn("second-run-log", text)
        self.assertEqual(text.count("ended"), 2)
