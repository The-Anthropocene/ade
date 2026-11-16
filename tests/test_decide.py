# id: tests.test_decide
# what: the decision queue (questions with rec, default, deadline), and drift at pick
# cmd: python3 -m unittest tests/test_decide.py
# reads: tests/fixture_repo
# writes: temporary directories only
import json
import os
import subprocess
from unittest import mock
from datetime import date, timedelta

from ade import config, q, records
from ade.stages import lane_path
from ade.verbs import decide, status  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase


class DecideTest(FixtureCase):
    def test_first_sentence_ends_at_terminal_punctuation_and_never_exceeds_the_limit(self):
        # Guards the one helper that briefs and the handover share: same cut rule, bounded length.
        self.assertEqual(decide.first_sentence("Use X! Then Y."), "Use X!")
        self.assertEqual(decide.first_sentence("Only one."), "Only one.")
        cut = decide.first_sentence("word " * 60 + "end. More.", limit=40)
        self.assertEqual(len(cut), 40)
        self.assertTrue(cut.endswith("…"))

    def test_a_retired_ruling_leaves_the_handover_but_stays_listed_and_questions_cannot_be_retired(self):
        # Guards the contract: retiring keeps the record (with its why) and removes it from what roles are handed.
        self.install()
        self.ade("decide", "--path", "app/*", "use zebra stripes")
        self.ade("decide", "--path", "app/*", "use dots instead")
        self.ade("decide", "--ask", "--rec", "a", "--default", "a", "--by", "7d", "which?")
        # Also guards: who acted never aborts a close or retire where getpass raises (no USER, no passwd entry).
        self.ade("decide", "--path", "app/*", "use plain paint")
        with mock.patch("getpass.getuser", side_effect=KeyError("uid")):
            code, out = self.ade("decide", "--retire", "d4", "--why", "x")
        self.assertEqual(code, 0, out)
        self.assertEqual(records.merged(config.load(str(self.repo)), "decision")["d4"]["retired_by"], "unknown")
        self.assertEqual(self.ade("decide", "--retire", "d3", "--why", "x")[0], 2)
        self.assertEqual(self.ade("decide", "--retire", "d99", "--why", "x")[0], 2)
        code, out = self.ade("decide", "--retire", "d1", "--why", "stripes clash", "--by", "d2")
        self.assertEqual(code, 2)
        self.assertIn("decide.incomplete", out)
        code, out = self.ade("decide", "--retire", "d1", "--why", "stripes clash", "--superseded-by", "d2")
        self.assertEqual(code, 0, out)
        ctx = config.load(str(self.repo))
        d1 = records.merged(ctx, "decision")["d1"]
        self.assertEqual((d1["superseded_by"], d1["why"], decide.state(d1)), ("d2", "stripes clash", "retired"))
        self.assertNotIn("zebra", self.ade("handover")[1])
        self.assertIn("zebra", self.ade("decide", "--list")[1])
        self.assertEqual(self.ade("decide", "--retire", "d1", "--why", "again")[0], 2)
        # Guards the contract: a retired ruling takes no answer, and the refusal writes nothing.
        before = self.ade("decide", "--list")[1]
        code, out = self.ade("decide", "--answer", "d1", "yes")
        self.assertEqual(code, 2)
        self.assertIn("decide.incomplete", out)
        self.assertEqual(self.ade("decide", "--list")[1], before)

    def test_a_question_waits_in_the_queue_until_answered_or_its_deadline_passes(self):
        self.install()
        self.assertEqual(self.ade("decide", "--ask", "which store?")[0], 2)  # rec, default and deadline are required
        code, out = self.ade("decide", "--ask", "--path", "app/*", "--rec", "sqlite", "--default", "jsonl",
                             "--by", "7d", "which store for calc history?")
        self.assertEqual(code, 0, out)
        self.ade("decide", "--ask", "--rec", "yes", "--default", "no", "--by", "2d", "ship the starter?")
        self.ade("decide", "--path", "app/*", "calc stays pure: no I/O")
        code, out = self.ade("decide", "--queue")
        self.assertEqual(out.splitlines()[0].split()[0], "d2")  # nearest deadline first
        # Guards the contract: bare `ade decide` is the queue; a ruling without --path still refuses.
        self.assertEqual(self.ade("decide")[1], out)
        self.assertEqual(self.ade("decide", "a ruling with no path")[0], 2)

        # Open questions reach the edit briefing, the status line and the handover.
        event = {"session_id": "s1", "cwd": str(self.repo), "tool_input": {"file_path": "app/calc.py"}}
        brief = json.loads(self.ade("hook", "brief", stdin=json.dumps(event))[1])["hookSpecificOutput"]
        self.assertIn("which store for calc history?", brief["additionalContext"])
        self.assertIn("2 to decide", self.ade("status", "--line")[1])
        self.assertIn("## Questions waiting on a person\n- d2", self.ade("handover")[1])

        self.assertEqual(self.ade("decide", "--answer", "d3", "no")[0], 2)  # d3 is a ruling, not a question
        self.assertEqual(self.ade("decide", "--answer", "d1", "sqlite")[0], 0)
        ctx = config.load(str(self.repo))
        qs = records.merged(ctx, "decision")
        self.assertEqual(decide.line(qs["d1"]), "d1 [app/*] which store for calc history? -> sqlite")
        # Guards the contract: an answer row records who answered; an exact engine marker (codex before claude-code) means agent, never a CLAUDE_CODE_* prefix.
        self.assertIn(qs["d1"]["by"], ("agent", "person", "unknown"))
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}):
            self.assertEqual(decide.answerer(), {"by": "agent", "engine": "codex"})
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}), mock.patch.dict(os.environ, clear=False):
            for n in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE"):
                os.environ.pop(n, None)
            self.assertNotEqual(decide.answerer()["by"], "agent")
        later = date.today() + timedelta(days=3)
        self.assertEqual(decide.state(qs["d2"], later), "defaulted")
        self.assertEqual([d["id"] for d in decide.queue(ctx, later)], [])
        self.assertIn("1 to decide", self.ade("status", "--line")[1])
        # Guards the contract: a question naming a card without --card warns but is still recorded; with --card it is silent.
        code, out = self.ade("decide", "--ask", "--rec", "y", "--default", "n", "--by", "2d", "land c12?")
        self.assertEqual(code, 0, out)
        self.assertIn("note:", out)
        code, out = self.ade("decide", "--ask", "--rec", "y", "--default", "n", "--by", "2d", "--card", "c12", "land c12?")
        self.assertNotIn("note:", out)

    def test_a_question_without_deadline_or_default_is_queued_after_dated_ones_and_never_crashes_a_reader(self):
        # Guards the contract: a held card's question has no deadline and no default, yet every reader of the queue works.
        self.install()
        self.ade("decide", "--ask", "--rec", "A", "--default", "A", "--by", "9d", "dated?")
        self.assertEqual(self.ade("card", "add", "held one", "--path", "app/*", "--done", "x", "--hold", "wait")[0], 0)
        self.ade("card", "add", "later hold", "--path", "app/*", "--done", "x")
        self.assertEqual(self.ade("card", "hold", "c2", "later")[0], 0)
        ctx = config.load(str(self.repo))
        self.assertEqual([d["id"] for d in decide.queue(ctx)], ["d1", "d2", "d3"])
        self.assertIsNone(decide.queue(ctx)[1].get("deadline"))
        self.assertEqual(self.ade("handover")[0], 0)
        # Guards the next-hops contract: card show ends with one capped 'next:' line that names the held card's open question.
        for _ in range(10):
            q.store(ctx, "x", cmd="t", card="c2")
        last = self.ade("card", "show", "c2")[1].strip().splitlines()[-1]
        self.assertTrue(last.startswith("next: ") and "d3" in last and len(last[6:].split("; ")) <= 8)
        self.assertEqual(self.ade("decide", "--queue")[0], 0)

    def test_a_landed_or_closed_card_settles_its_park_question_but_not_its_ruling(self):
        # Guards the rule shared with the briefing filter: a question tied to a landed or closed card is settled;
        # a ruling naming the card still governs.
        self.install()
        ctx = config.load(str(self.repo))
        for cid, end in (("c900", "landed"), ("c901", "closed")):
            records.append(ctx, "card", {"id": cid, "status": "open"})
            q = records.append(ctx, "decision", {"text": "park?", "paths": [], "kind": "question", "card": cid,
                                                 "rec": "a", "default": "a",
                                                 "deadline": (date.today() + timedelta(days=5)).isoformat()}, prefix="d")
            r = records.append(ctx, "decision", {"text": "rule", "paths": ["x/*"], "kind": "ruling", "card": cid},
                               prefix="d")
            self.assertIn(q["id"], [d["id"] for d in decide.queue(ctx)])
            records.append(ctx, "card", {"id": cid, "status": end})
            self.assertNotIn(q["id"], [d["id"] for d in decide.queue(ctx)])
            self.assertIn(r["id"], [d["id"] for d in decide.unsettled(ctx)])

    def test_bad_arguments_share_one_code_but_each_keeps_its_own_cure_and_show_prints_line_then_text(self):
        # Guards the contract: a verb's bad-argument refusals share a code (budget headroom) yet print distinct cures.
        self.install()
        code, bad_by = self.ade("decide", "--ask", "--rec", "a", "--default", "b", "--by", "someday", "q?")
        code2, bad_answer = self.ade("decide", "--answer", "d99", "x")
        self.assertEqual((code, code2), (2, 2))
        for out in (bad_by, bad_answer):
            self.assertIn("decide.incomplete", out)
        self.assertNotEqual(bad_by, bad_answer)  # each refusal prints its own cure
        # Also guards: --show ID prints decide.line then the whole text; an unknown id refuses with the --list cure.
        self.ade("decide", "--path", "app/*", "calc stays pure: no I/O")
        ctx = config.load(str(self.repo))
        d = records.merged(ctx, "decision")["d1"]
        code, out = self.ade("decide", "--show", "d1")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [decide.line(d)])  # the line already carries the text; show must not repeat it
        code, out = self.ade("decide", "--show", "d99")
        self.assertEqual(code, 2)
        self.assertIn("decide.incomplete", out)
        # Also guards: a land question refuses an answer land cannot read and stays open; 'rework: x' is then taken.
        self.ade("decide", "--ask", "--rec", "land", "--default", "land", "--by", "2d", "land c9?")
        code, out = self.ade("decide", "--answer", "d2", "rework (small): x")
        self.assertEqual(code, 2)
        self.assertEqual(decide.state(records.merged(config.load(str(self.repo)), "decision")["d2"]), "open")
        self.assertEqual(self.ade("decide", "--answer", "d2", "rework: x")[0], 0)
        self.ade("decide", "--ask", "--rec", "land", "--default", "land", "--by", "2d", "land c8?")
        self.assertEqual(self.ade("decide", "--answer", "d3", "drop")[0], 0)
        # Also guards: every machine-read question (FORMS) refuses a typo, lists its forms, and takes a real form.
        self.ade("decide", "--ask", "--rec", "retry", "--default", "retry", "--by", "2d", "parked?")
        code, out = self.ade("decide", "--answer", "d4", "retyr")
        self.assertEqual(code, 2)
        self.assertIn("decide.incomplete", out)
        self.assertEqual(self.ade("decide", "--answer", "d4", "retry: x")[0], 0)
        # Also guards: an accept question (a drafted card) takes 'retry: x', the redraft form conduct reads.
        self.ade("decide", "--ask", "--rec", "accept", "--default", "accept", "--by", "2d", "draft?")
        self.assertEqual(self.ade("decide", "--answer", "d5", "retry: x")[0], 0)

    def test_importing_twice_adds_each_row_once(self):
        # Guards the contract: a row's source is imported once; a re-run adds nothing and a bad row refuses before any write.
        self.install()
        f = self.repo / "r.jsonl"
        f.write_text('{"text": "a", "paths": ["app/*"], "source": "S1"}\n{"text": "b", "paths": [], "source": "S2", "date": "2026-01-01"}\n')
        self.assertEqual(self.ade("decide", "--import", str(f))[0], 0)
        self.assertEqual(len(records.merged(config.load(str(self.repo)), "decision")), 2)
        self.assertEqual(self.ade("decide", "--import", str(f))[0], 0)
        f.write_text('{"text": "c", "source": "S3"}\n{"text": "d"}\n')
        code, out = self.ade("decide", "--import", str(f))
        self.assertEqual(code, 2)
        self.assertEqual(len(records.merged(config.load(str(self.repo)), "decision")), 2)

    def test_the_pick_shows_paths_that_moved_since_the_card_was_written(self):
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        self.ade("card", "add", "readme", "--path", "README.md", "--done", "reworded")
        calc = self.repo / "app/calc.py"
        calc.write_text(calc.read_text() + "\n# moved on main after the card was written\n")
        self.git("commit", "-qam", "change calc on main")
        listing = self.ade("card", "list")[1].splitlines()
        self.assertTrue(listing[0].endswith("(moved)") and not listing[1].endswith("(moved)"), listing)
        self.assertIn("app/calc.py", self.ade("loop", "c1")[1].splitlines()[0])

    def parked_lane(self):
        self.install()
        ctx = config.load(self.repo)
        lp = lane_path(ctx, "c1")
        self.git("worktree", "add", "-q", "-b", "ade/c1", str(lp))
        (lp / "new_file.txt").write_text("new\n")
        readme = lp / "README.md"
        readme.write_text(readme.read_text() + "\nedited in the lane\n")
        row = records.append(ctx, "decision", {"text": "c1 parked", "paths": [], "kind": "question", "rec": "retry",
                                               "default": "retry", "deadline": date.today().isoformat(), "card": "c1"}, prefix="d")
        return ctx, lp, row["id"]

    def test_drop_saves_the_lane_as_an_applicable_patch_and_removes_it_while_retry_keeps_it(self):
        # Guards the contract: a drop answer never loses lane work (the patch applies on trunk) and only a drop removes the lane.
        ctx, lp, d = self.parked_lane()
        self.assertEqual(self.ade("decide", "--answer", d, "retry")[0], 0)
        self.assertTrue(lp.exists() and "ade/c1" in self.git("branch", "--list", "ade/c1"))
        code, out = self.ade("decide", "--answer", d, "drop the card")
        self.assertEqual(code, 0, out)
        patch = ctx.state / "logs" / "c1.dropped.patch"
        self.assertIn(str(patch), out)
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=self.repo, check=True)
        self.assertIn("new_file.txt", patch.read_text())
        self.assertFalse(lp.exists())
        self.assertEqual(self.git("branch", "--list", "ade/c1"), "")
        # Closing a card with a lane does the same: the patch is kept, the lane and branch go.
        self.ade("card", "add", "t", "--path", "app/*", "--done", "d")
        self.git("worktree", "add", "-q", "-b", "ade/c1", str(lp))
        patch.unlink()
        self.assertEqual(self.ade("card", "close", "c1", "--why", "no")[0], 0)
        self.assertTrue(patch.exists())
        self.assertFalse(lp.exists())
