# id: tests.test_loop
# what: one full loop on the fixture with the fake provider, and a loop that waits on a dirty main then resumes
# cmd: python3 -m unittest tests/test_loop.py
# reads: tests/fixture_repo
# writes: temporary directories only
import json
import os

from ade import config, records, wait
from ade.verbs import card, cockpit, decide, handover, land, loop  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase


class LoopTest(FixtureCase):
    def test_held_never_locks_its_data_file(self):
        # Guards the host.lock contract: Windows locks are mandatory, so held() must lock a sibling file,
        # never the lock data file it reads and writes.
        from unittest import mock
        from ade import host
        self.install()
        ctx = config.load(str(self.repo))
        locked = []
        real = host.lock
        with mock.patch.object(host, "lock", side_effect=lambda p: (locked.append(p), real(p))[1]):
            with loop.held(ctx, "c1"):
                pass
        self.assertTrue(locked)
        self.assertNotIn(ctx.state / "lanes" / "c1.lock", locked)

    def add_card(self):
        code, out = self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        self.assertEqual(code, 0, out)
        return out.split()[0]

    def records(self, rtype):
        rows = [json.loads(x) for x in (self.repo / ".ade/records.jsonl").read_text().splitlines()]
        return [r for r in rows if r["type"] == rtype]

    def test_handover_caps_path_bound_rulings(self):
        # Guards that many rulings keep the handover under budget and every path-free ruling stays in full.
        self.install()
        ctx = config.load(str(self.repo))
        # Guards that only open cards are listed: closed and landed ones never reach a seat.
        for cid, st in (("c9001", "closed"), ("c9002", "landed")):
            records.append(ctx, "card", {"id": cid, "title": "gone", "paths": [], "done": "x", "status": st})
        self.assertNotIn("- c9001 ", handover.text(ctx, "pilot"))
        self.assertNotIn("- c9002 ", handover.text(ctx, "pilot"))
        base = len(handover.text(ctx, "builder")) // 4
        for i in range(400):
            paths = [] if i % 10 == 0 else [f"app/m{i}/*"]
            records.append(ctx, "decision", {"text": f"Ruling {i} first. Then a long second sentence " * 3,
                                             "paths": paths, "kind": "ruling"}, prefix="d")
        rows = records.merged(ctx, "decision").values()
        full = sum(len(f"- {decide.line(d)}\n") for d in rows if not d["paths"]) // 4
        # Room for the board, the path-free rulings and the bound rulings' quarter; too little for every bound one.
        ctx.cfg["budgets"]["handover_tokens"] = 2 * (base + full)
        body = handover.text(ctx, "builder")
        self.assertLessEqual(len(body) // 4, ctx.cfg["budgets"]["handover_tokens"])
        for d in rows:
            if not d["paths"]:
                self.assertIn(d["text"], body)
        self.assertIn("more path-bound rulings", body)

    def test_loop_waits_on_dirty_main_then_resumes_from_cache(self):
        self.install()
        cid = self.add_card()
        calc = self.repo / "app/calc.py"
        calc.write_text(calc.read_text() + "# local edit\n")  # a path the lane changes: landing waits
        # Guards the --json contract an outer loop reads: a stop ends with one JSON object (card, stage, outcome,
        # next), and a stop for judgement adds the open question with its recommendation, default and deadline.
        self.ade("decide", "--ask", "which?", "--rec", "A", "--default", "A", "--by", "7d")
        code, out = self.ade("loop", cid, "--json")
        row = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(code, 3, out)
        self.assertLessEqual({"card", "stage", "outcome", "next"}, row.keys())
        self.assertEqual(row["card"], cid)
        full = json.loads(loop.stop_line(config.load(str(self.repo)), cid, "land", "needs-judgement", "x"))
        self.assertLessEqual({"question", "recommendation", "default", "deadline"}, full.keys())
        code, out = self.ade("loop", cid)
        self.assertEqual(code, 3, out)
        self.assertIn("next:", out)
        self.assertEqual(self.records("journal")[-1]["outcome"], "wait")
        self.git("checkout", "app/calc.py")
        code, out = self.ade("loop", cid)
        self.assertEqual(code, 0, out)
        self.assertIn("already built", out)
        self.assertIn("cached", out)
        # Guards one run per card: the lock frees on exit, and a live holder's lock refuses a second run naming its pid.
        lock = config.load(str(self.repo)).state / "lanes" / f"{cid}.lock"
        self.assertEqual(lock.read_text(), "")
        holder = os.getppid()
        lock.write_text(json.dumps([holder, wait.started(holder)]))
        code, out = self.ade("loop", cid)
        self.assertNotEqual(code, 0)
        self.assertIn("lane-in-use", out)
        self.assertIn(f"ade wait exit {holder}", out)

    def test_card_show_keeps_a_passing_test_to_one_line_and_a_failing_one_whole(self):
        # Guards that a passing test row never prints its output tail (it can read like a live refusal); a failing one does.
        self.install()
        cid = self.add_card()
        ctx = config.load(str(self.repo))
        for result, shown in (("pass", False), ("fail", True)):
            records.append(ctx, "test", {"card": cid, "tests": "pytest x", "result": result, "tail": "TAILMARK"})
            code, out = self.ade("card", "show", cid)
            self.assertEqual(code, 0, out)
            self.assertEqual("TAILMARK" in out, shown, out)

    def test_readers_survive_a_journal_row_without_stage_fields_and_a_landed_row_without_commit(self):
        # Guards that handover, status, the cockpit and card show never index stage/outcome/note on a row that lacks them.
        self.install()
        cid = self.add_card()
        records.append(config.load(str(self.repo)), "journal", {"card": cid, "wait": "clean main"})
        for verb in (("handover",), ("status",), ("cockpit",), ("card", "show", cid)):
            code, out = self.ade(*verb)
            self.assertNotIn("Traceback", out, verb)
        # Guards that a landed card row carrying commit: null reports '?' instead of crashing the landed line.
        records.append(config.load(str(self.repo)), "card", {"id": cid, "status": "landed", "commit": None})
        code, out = self.ade("loop", cid)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{cid} already landed at ?", out)
