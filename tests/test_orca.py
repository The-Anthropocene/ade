# id: tests.test_orca
# what: Orca mode mirrors lanes, stops and the board into Orca, and a missing Orca never stops the loop; ade watch
# cmd: python3 -m unittest tests/test_orca.py
# reads: tests/fixture_repo
# writes: temporary directories only
import contextlib
import json
import os
import shutil
import subprocess
from unittest import mock

from ade import config, orca, records
from ade.verbs import cockpit, loop, watch  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase


@contextlib.contextmanager
def _both(*patches):
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


class OrcaTest(FixtureCase):
    def orca_mode(self):
        self.install()
        toml = self.repo / "ade.toml"
        toml.write_text(toml.read_text().replace('mode = "cli"', 'mode = "orca"'))
        self.git("commit", "-qam", "cockpit in orca mode")

    def fake_orca(self, terminals=()):
        """Stand in for the orca CLI and its node runtime client only (git still runs for real) and record every
        call: CLI calls in `calls`, runtime-client calls in `self.rpcs`, the repo's imported lanes in `self.imported`."""
        calls, real, real_which = [], subprocess.run, shutil.which
        self.rpcs, self.imported = [], []

        def run(argv, *a, **k):
            if argv[0] == "/x/node":
                method, params = argv[-2], json.loads(argv[-1])
                self.rpcs.append(method)
                self.imported[:] = params["updates"]["importedExternalWorktreePaths"]
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[0] != "/x/orca":
                return real(argv, *a, **k)
            calls.append(argv[1:])
            result = {"terminals": [{"title": t} for t in terminals]} if argv[1:3] == ["terminal", "list"] else {}
            if argv[1] == "status":
                result = {"runtime": {"reachable": True}}
            if argv[1:3] == ["repo", "list"]:
                result = {"repos": [{"id": "r1", "path": str(self.repo), "importedExternalWorktreePaths": list(self.imported)}]}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "result": result}), stderr="")
        # shutil is one module everywhere, so only orca's own names may resolve to the stand-in: on Windows the
        # provider and test stages resolve their commands through shutil.which too.
        stands_in = {"orca": "/x/orca", "orca-ide": "/x/orca", "orca-dev": "/x/orca", "node": "/x/node"}
        which = mock.patch("shutil.which", side_effect=lambda name, *a, **k: stands_in.get(name) or real_which(name, *a, **k))
        client = mock.patch.dict("os.environ", {"ORCA_CLI_COMMAND": "orca", "ORCA_RUNTIME_CLIENT": "/x/runtime-client.js"})
        return calls, mock.patch("subprocess.run", side_effect=run), _both(which, client)

    def test_a_loop_in_orca_mode_shows_its_lane_terminal_and_landing(self):
        self.orca_mode()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        calls, run, which = self.fake_orca()
        with run, which:
            self.assertTrue(orca.reachable())
            code, out = self.ade("loop", "c1")
        self.assertEqual(code, 0, out)
        self.assertTrue(all(c[-1] == "--json" for c in calls), calls)
        # The invariants, not Orca's call order: the repo is added before its lane terminal is made, the lane (kept
        # outside the repo folder) is imported for the run and forgotten at land, and the terminal watches the lane.
        verbs = [" ".join(c[:2]) for c in calls]
        self.assertLess(verbs.index("repo add"), verbs.index("terminal create"))
        self.assertEqual(self.rpcs, ["repo.update", "repo.update"])
        self.assertEqual(self.imported, [])
        create = calls[verbs.index("terminal create")]
        self.assertIn("ade c1", create)
        self.assertTrue(create[create.index("--command") + 1].endswith(" watch c1 --until-landed"), create)
        self.assertTrue(any("c1 landed" in c for c in calls), calls)

    def test_a_stop_becomes_the_worktree_comment_and_terminals_are_not_doubled(self):
        self.orca_mode()
        self.ade("card", "add", "anything under app", "--path", "app/*", "--done", "done")
        calls, run, which = self.fake_orca(terminals=["ade c1"])
        with run, which:
            code, out = self.ade("loop", "c1")
        self.assertEqual(code, 3, out)  # the fake builder has no literal path to edit
        self.assertNotIn(["terminal", "create"], [c[:2] for c in calls])
        comment = calls[-1][calls[-1].index("--comment") + 1]
        self.assertTrue(comment.startswith("c1 needs-judgement at build"), comment)
        self.assertIn("next: ", comment)

    def test_the_board_opens_in_an_orca_terminal_and_auto_mode_only_when_orca_is_running(self):
        self.install()
        calls, run, which = self.fake_orca()
        with run, which:
            code, out = self.ade("cockpit", "--mode", "orca")
        self.assertEqual(code, 0, out)
        self.assertIn("ade cockpit", calls[-1])
        command = calls[-1][calls[-1].index("--command") + 1]
        self.assertTrue(command.endswith("cockpit --follow --mode cli"), command)  # live, and never recursing into Orca mode
        calls, run, which = self.fake_orca()  # _both is one-shot, so auto mode gets a fresh stand-in
        with run, which:
            self.assertEqual(self.ade("cockpit", "--mode", "auto")[0], 0)
        self.assertIn("ade cockpit", calls[-1])
        real_which = shutil.which
        with mock.patch("shutil.which", side_effect=lambda name, *a, **k: None if name.startswith("orca") else real_which(name, *a, **k)):
            code, out = self.ade("cockpit", "--mode", "auto")
        self.assertEqual(code, 0, out)  # no Orca: the terminal board alone, no refusal
        self.assertNotIn("orca", out)
        # The flight strip at loop start (orca.strip): guards the three branches and the [cockpit] band setting.
        ctx, opened = config.load(self.repo), []
        launch = lambda c, command: opened.append(command) or "term"
        codex, claude = {"CODEX_THREAD_ID": "t"}, {"CLAUDECODE": "1"}
        ctx.cfg["cockpit"]["band"] = "auto"  # the default is now "terminal"; "auto" keeps Claude Code out
        with mock.patch("ade.orca.reachable", return_value=False):
            orca.strip(ctx, claude, launch)  # band = "auto", a plain Claude Code pilot: nothing
            self.assertEqual(opened, [])
            orca.strip(ctx, codex, launch)  # another engine: one new terminal running the strip
            self.assertTrue(opened[0].endswith("cockpit --follow --mode cli"), opened)
            ctx.cfg["cockpit"]["band"] = "terminal"
            (self.repo / ".ade" / "cockpit.pid").unlink(missing_ok=True)  # the first launch left a fresh claim
            orca.strip(ctx, claude, launch)  # band = "terminal" reaches Claude Code too
            self.assertEqual(len(opened), 2)
            (self.repo / ".ade").mkdir(exist_ok=True)
            (self.repo / ".ade" / "cockpit.pid").write_text(str(os.getpid()))
            orca.strip(ctx, codex, launch)  # a live cockpit terminal is open: it redraws itself
            self.assertEqual(len(opened), 2)
            (self.repo / ".ade" / "cockpit.pid").unlink()
            ctx.cfg["cockpit"]["band"] = "off"
            orca.strip(ctx, codex, launch)
            self.assertEqual(len(opened), 2)
            ctx.cfg["cockpit"]["band"] = "auto"
            orca.strip(ctx, codex, lambda c, command: None)  # no terminal found: one line, no refusal
        with mock.patch("ade.orca.reachable", return_value=True), mock.patch("ade.orca.board") as board:
            orca.strip(ctx, codex, launch)  # Orca running: its terminal, no other
        self.assertEqual((board.call_count, len(opened)), (1, 2))
        # Windows (faked): _alive needs the exact quoted pid field; _open passes the root as cwd, not in a shell string (guards both fixes).
        csv = '"python.exe","1234","Console","1","9,000 K"\n'
        with mock.patch("sys.platform", "win32"), mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=csv)
            self.assertEqual((orca._alive(1234), orca._alive(123), orca._alive(9)), (True, False, False))
            orca._open(ctx, '"C:/x/ade" cockpit --follow --mode cli')
        self.assertEqual(popen.call_args[0][0], 'cmd /k "title ade cockpit && "C:/x/ade" cockpit --follow --mode cli"')
        self.assertEqual(popen.call_args[1]["cwd"], ctx.root)
        self.assertEqual(popen.call_args[1]["creationflags"], getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        # macOS (faked): an 'open' that exits non-zero (blocked by a sandbox) is no terminal, so strip prints the fallback line.
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.Popen") as popen, mock.patch("builtins.print") as out:
            popen.return_value.wait.return_value = 1
            orca.strip(ctx, codex)
        self.assertIn("run in another one", out.call_args[0][0])

    def test_the_board_shows_a_held_card_with_its_release_command_and_follow_redraws_until_interrupted(self):
        # Guards the contract: a card added --hold reads "held" on the board, with the command that frees it, as in `card list`.
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "x", "--hold", "waits")
        qid = next(d["id"] for d in records.merged(config.load(str(self.repo)), "decision").values() if d.get("card") == "c1")
        row = next(l for l in self.ade("cockpit", "--mode", "cli")[1].splitlines() if l.strip().startswith("c1 "))
        self.assertIn("held", row)
        self.assertIn(f"ade decide --answer {qid} retry", row)
        self.assertNotIn("ade loop", row)
        # Guards the contract: --follow redraws the board every poll and Ctrl-C ends it with exit 0.
        sleeps = iter([None, KeyboardInterrupt])

        def sleep(_):
            r = next(sleeps)
            if r:
                raise r
        with mock.patch("ade.verbs.cockpit.time.sleep", sleep):
            code, out = self.ade("cockpit", "--follow")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("ADE cockpit"), 2)

    def test_the_cockpit_strip_reads_stages_from_records(self):
        # Guards the format: a flying card shows ✓ passed, ■ under way, ○ not reached, one stage per symbol, from records alone.
        self.install()
        ctx = config.load(str(self.repo))
        records.append(ctx, "flight", {"flight": "f1", "card": "c1"})
        records.append(ctx, "journal", {"card": "c1", "stage": "lane", "outcome": "advance", "note": "x"})
        self.assertEqual(cockpit.strips(ctx), ["c1  ✓ lane  ■ build  ○ review  ○ test  ○ land"])
        # Byte-for-byte twin of the flight band's line(): title, two spaces between marks, ✗ for any other outcome.
        records.append(ctx, "card", {"id": "c1", "title": "Fly", "paths": ["a"], "done": "d"})
        records.append(ctx, "journal", {"card": "c1", "stage": "build", "outcome": "advance", "note": "x"})
        records.append(ctx, "journal", {"card": "c1", "stage": "review", "outcome": "retry", "note": "x"})
        self.assertEqual(cockpit.strips(ctx), ["c1 Fly  ✓ lane  ✓ build  ✗ review  ■ test  ○ land"])

    def test_watch_prints_the_lane_log_and_for_a_landed_card_without_a_log_where_it_landed(self):
        self.install()
        self.ade("card", "add", "anything under app", "--path", "app/*", "--done", "done")
        self.ade("loop", "c1")
        code, out = self.ade("watch", "c1", "--once")
        self.assertEqual(code, 0, out)
        self.assertIn("--- builder via fake ---\nfake builder: no literal path to edit", out)
        # Guards the contract: a landed card with no lane log prints one landed line and the log path, never silence.
        self.ade("card", "add", "anything else under app", "--path", "app/*", "--done", "done")
        records.append(config.load(str(self.repo)), "card", {"id": "c2", "status": "landed", "commit": "0123456789abcdef"})
        code, out = self.ade("watch", "c2")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "c2 landed at 0123456789; its log is .ade/logs/c2.log")
