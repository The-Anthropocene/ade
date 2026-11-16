# id: tests.test_status
# what: ade status lists lane worktrees as "card · age · dirty|clean"; --line stays a single line
# cmd: python3 -m unittest tests/test_status.py
# reads: tests/fixture_repo
# writes: temporary directories only
import re

from ade.verbs import status  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase

ROW = re.compile(r"^  c9 · \d+[smhd] · (clean|dirty)$")


class StatusLanesTest(FixtureCase):
    def lane(self):
        path = self.tmp / "lane-c9"
        self.git("worktree", "add", "-q", "-b", "ade/c9", str(path))
        self.install()
        return path

    def rows(self, *argv):
        code, out = self.ade("status", *argv)
        self.assertEqual(code, 0, out)
        return out.splitlines()

    def test_lane_rows_follow_the_format_and_line_form_and_laneless_status_print_only_the_one_liner(self):
        self.install()
        self.assertEqual(len(self.rows()), 1)
        from ade import config, records
        ctx = config.load(self.repo)
        ids = [records.append(ctx, "card", {"title": t, "paths": ["x"], "done": "d", "status": "open"}, "c")["id"]
               for t in ("live", "held", "gone")]
        records.append(ctx, "card", {"id": ids[2], "status": "closed"})
        records.append(ctx, "decision", {"card": ids[1], "kind": "question", "text": "q"}, "d")
        self.assertIn(" · 1 open · 1 held", self.rows("--line")[0])  # closed never counts; held shows apart
        path = self.lane()
        self.assertEqual(len(self.rows("--line")), 1)
        (self.repo / ".ade" / "lanes").mkdir(parents=True, exist_ok=True)  # a dead holder is named; none, nothing
        self.assertNotIn("cut off", self.ade("status")[1])
        (self.repo / ".ade" / "lanes" / "c9.lock").write_text("[999999999, null]")
        self.assertNotIn("cut off", self.ade("status")[1])  # no open card c9: a stale lock names nothing
        records.append(ctx, "card", {"id": "c9", "title": "t", "paths": ["x"], "done": "d", "status": "open"})
        self.assertIn("cut off: c9 (run 999999999 died at", self.ade("status")[1])
        records.append(ctx, "card", {"id": "c9", "status": "landed"})  # a landed card's stale lock is no cut-off run
        self.assertNotIn("cut off", self.ade("status")[1])
        (self.repo / ".ade" / "lanes" / "c9.lock").unlink()
        lines = self.rows()
        self.assertEqual(len(lines), 2, lines)
        self.assertRegex(lines[1], ROW)
        self.assertTrue(lines[1].endswith("clean"))
        (path / "scratch.txt").write_text("x\n")
        lines = self.rows()
        self.assertRegex(lines[1], ROW)
        self.assertTrue(lines[1].endswith("dirty"))


class GrepNotesLineTest(FixtureCase):
    # Guards the contract: each file shown counts once per session; a read counts only against a note shown in the
    # same session; bad rows are skipped; no counter file means no line.
    def test_counts_are_per_session_and_tolerate_bad_rows(self):
        import json

        from ade import config
        self.install()
        self.assertNotIn("grep notes", self.ade("status")[1])
        rows = [{"session": "a", "shown": ["x.py", "y.py"]}, {"session": "a", "shown": ["x.py"]}, "not json",
                {"shown": ["z.py"]}, {"session": "a", "read": "x.py"}, {"session": "a", "read": "x.py"},
                {"session": "b", "read": "y.py"},
                # a lane (cwd): a Bash cat of the file and a named-test read by another session both count
                {"session": "c", "cwd": "/l", "shown": ["m.py", "n.py", "o.py"], "tests": {"m.py": [], "n.py": ["tests/t.py"], "o.py": ["tests/u.py"]}},
                {"session": "r", "cwd": "/l", "read": "m.py"}, {"session": "r", "cwd": "/l", "read": "tests/t.py"}]
        state = config.load(self.repo).state
        state.mkdir(parents=True, exist_ok=True)
        (state / "grep-notes.jsonl").write_text(
            "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
        code, out = self.ade("status")
        self.assertEqual(code, 0, out)
        self.assertIn("grep notes: 5 noted, 3 acted on (60%): 2 opened the file, 1 opened a named test", out)
