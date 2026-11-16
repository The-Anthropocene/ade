# id: tests.test_home
# what: a bare `ade` prints the home screen, and it lists every verb a person runs, exactly once
# cmd: python3 -m unittest tests/test_home.py
# reads: ade/verbs
# writes: temporary directories only
import subprocess
import sys
from pathlib import Path

from ade import cli, config, home, records
from tests.helpers import FixtureCase


class HomeTest(FixtureCase):
    def test_bare_ade_lists_every_verb_once(self):
        listed = [cmd.split()[0] for _, rows in home.GROUPS for cmd, _ in rows]
        self.assertEqual(sorted(listed), sorted(set(cli.verbs()) - home.HIDDEN))
        code, out = self.ade()
        self.assertEqual(code, 0, out)
        for _, rows in home.GROUPS:
            for cmd, _ in rows:
                self.assertIn(f"ade {cmd}", out)
        self.assertIn("not set up here", out)  # the fixture has no ade.toml until init
        # Guards the contract: a first word that is no verb gets one line naming bare `ade` (and a near verb when there is one), exit 2, not argparse's wall; flags are untouched.
        code, out = self.ade("nonsuchverb")
        self.assertEqual(code, 2)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("run ade alone for the verb list", out)
        code, out = self.ade("cokpit")
        self.assertEqual(code, 2)
        self.assertIn("did you mean cockpit?", out)
        with self.assertRaises(SystemExit) as cm:
            self.ade("--version")
        self.assertEqual(cm.exception.code, 0)

    def test_set_up_home_says_what_to_do_next(self):
        # Guards the contract: a set-up folder's home screen has one `next:` line: the open card's loop, or the card-add line once none is open.
        empty = self.tmp / "new"
        empty.mkdir()
        self.assertEqual(self.ade("init", "--yes", cwd=empty)[0], 0)
        lines = [l for l in self.ade(cwd=empty)[1].splitlines() if l.startswith("next: ")]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("next: ade loop c1 (c1: "), lines)
        ctx = config.load(empty)
        records.append(ctx, "card", {**records.merged(ctx, "card")["c1"], "status": "landed"})
        lines = [l for l in self.ade(cwd=empty)[1].splitlines() if l.startswith("next: ")]
        self.assertEqual(lines, ["next: ade card add <title> --path <glob> --done <line>"])
        # Guards the contract: when every open card waits on a question, home still names a next step, `ade decide`.
        self.assertEqual(self.ade("card", "draft", "make a change", cwd=empty)[0], 0)
        lines = [l for l in self.ade(cwd=empty)[1].splitlines() if l.startswith("next: ")]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("next: ade decide ("), lines)

    def test_closed_pipe_ends_quietly(self):
        # Guards the contract: a reader that closes early (`ade ... | head`) leaves no traceback on stderr.
        empty = self.tmp / "new"
        empty.mkdir()
        self.assertEqual(self.ade("init", "--yes", cwd=empty)[0], 0)
        p = subprocess.Popen([sys.executable, str(Path(cli.__file__).parent.parent / "bin" / "ade"), "card", "list"],
                             cwd=empty, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p.stdout.close()
        self.assertNotIn("Traceback", p.stderr.read())
        p.wait()
