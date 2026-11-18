# id: tests.test_conduct
# what: the conductor's invariants: a clean board lands with no model woken, a card added mid-run is flown in the same run, cards that meet in the graph never fly together, a renumbered card is never parked under another machine's id, a pilot fix is committed in its lane, a land question lands only on an answer, and a script naming a verb that is not there fails the graph check
# cmd: python3 -m unittest tests/test_conduct.py
# reads: tests/fixture_repo
# writes: temporary directories only
import json
from datetime import timedelta
from unittest import mock

from ade import config, route  # noqa: F401  (route imported so the graph records coverage)
from ade import records, stages
from ade.verbs import conduct, decide
from tests.helpers import FixtureCase


class ConductTest(FixtureCase):
    def test_clean_board_lands_without_waking_a_model(self):
        self.install()
        for title in ("note the calc contract", "note it again"):
            self.assertEqual(self.ade("card", "add", title, "--path", "app/calc.py", "--done", "comment added")[0], 0)
        code, out = self.ade("conduct")
        self.assertEqual(code, 0, out)
        rows = [json.loads(x) for x in (self.repo / ".ade/records.jsonl").read_text().splitlines()]
        cards = {r["id"]: r["status"] for r in rows if r["type"] == "card"}
        self.assertEqual(set(cards.values()), {"landed"})
        self.assertFalse([r for r in rows if r["type"] == "usage" and r["role"] == "pilot"])  # judgement never needed
        # Guards the contract: one flight id per run, a flight row per card, and every usage row of the run stamped with it.
        flights = [r for r in rows if r["type"] == "flight"]
        self.assertEqual({r["card"] for r in flights}, {"c1", "c2"})
        self.assertEqual({r["flight"] for r in flights}, {"f1"})
        self.assertEqual({r.get("flight") for r in rows if r["type"] == "usage"} - {None}, {"f1"})
        self.assertIn("f1: c1, built by", self.ade("telemetry", "--flights")[1])
        # Also guards the contract: a stop conduct retried is one journal row (stage conduct, outcome retried) naming the
        # stop and what was done; the end line names it beside the verdict and `card show` lists it.
        self.ade("card", "add", "note it a third time", "--path", "app/calc.py", "--done", "comment added")
        real, blips = stages.FUNCS["build"], []

        def blip(ctx, c):  # the first build exits badly (the route retries it), the second builds
            blips.append(c["id"])
            return real(ctx, c) if len(blips) > 1 else stages.Result("needs-judgement", "[provider.failed] exited 1\nlog", "x")

        with mock.patch.dict(stages.FUNCS, {"build": blip}):
            code, out = self.ade("conduct")
        self.assertEqual(code, 0, out)
        self.assertIn("c3 landed (retried:", out)
        self.assertIn("retried", self.ade("card", "show", "c3")[1])

    def test_a_card_added_while_conduct_runs_is_flown_unprompted(self):
        # Guards the contract: a card added mid-run is flown in the same run, and --follow runs again after its poll
        # sleep, so a card added between rounds lands too.
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        real = conduct.conduct

        def first_adds_another(ctx, c, rt):
            if c["id"] == "c1":
                self.ade("card", "add", "note it again", "--path", "app/calc.py", "--done", "comment added")
            return real(ctx, c, rt)

        with mock.patch.object(conduct, "conduct", first_adds_another):
            code, out = self.ade("conduct")
        self.assertEqual(code, 0, out)
        self.assertIn("c2 landed", out)

        def adds_another(_secs):
            self.ade("card", "add", "note it a third time", "--path", "app/calc.py", "--done", "comment added")

        with mock.patch("time.sleep", adds_another):
            code, out = self.ade("conduct", "--follow", "--rounds", "2")
        self.assertEqual(code, 0, out)
        self.assertIn("c3 landed", out)

    def test_cards_that_meet_in_the_graph_never_fly_together(self):
        self.install()
        for title, path in (("calc", "app/calc.py"), ("calc again", "app/calc.py"), ("its test", "tests/test_calc.py"),
                            ("notes", "docs/notes.md"), ("a new module", "lib/new.py")):
            self.ade("card", "add", title, "--path", path, "--done", "done")
        ctx = config.load(str(self.repo))
        cards = list(records.merged(ctx, "card").values())
        # c2 shares c1's file and c3 is the test that reaches it; prose and an unknown folder meet nothing.
        self.assertEqual([c["id"] for c in conduct.free(ctx, cards, set())], ["c1", "c4", "c5"])
        self.assertEqual([c["id"] for c in conduct.free(ctx, cards, {"c3"})], ["c4", "c5"])

    def test_a_lost_card_is_found_by_more_than_its_title(self):
        # Guards: _now finds a lost card by title, paths and done, never mistaking another that merely shares the title.
        ctx = mock.Mock(renumbered={})
        ours = {"id": "c1", "title": "t", "paths": ["a.py"], "done": "d"}
        other = {**ours, "id": "c2", "paths": ["b.py"]}
        moved = {**ours, "id": "c3"}
        with mock.patch.object(records, "merged", lambda c, k: {"c2": other, "c3": moved}):
            self.assertEqual(conduct._now(ctx, ours)["id"], "c3")
        with mock.patch.object(records, "merged", lambda c, k: {"c2": other}):
            self.assertIsNone(conduct._now(ctx, ours))

    def test_a_card_renumbered_by_its_own_landing_is_reported_and_parked_under_the_new_id_only(self):
        # Guards the contract: when the push renumbers our card because another machine minted the same id, the
        # conductor acts on the new id and never writes a hold or park question on the other machine's card.
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        ctx = config.load(str(self.repo))
        ours = records.merged(ctx, "card")["c1"]

        def push_renumbers(c, line, card):  # the other machine's c1 takes the id; ours becomes c2
            records.append(c, "card", {**{k: v for k, v in ours.items() if k not in ("type", "at")}, "id": "c2"})
            records.append(c, "card", {**{k: v for k, v in ours.items() if k not in ("type", "at")},
                                       "title": "the other machine's card"})
            c.renumbered["c1"] = "c2"

        with mock.patch.object(conduct, "_run", push_renumbers), mock.patch("builtins.print") as shown:
            verdict = conduct.conduct(ctx, ours, {"start": {"run": "ade x"}, "stop": []})
        rows = [r for r in records.read(ctx, "hold") + records.read(ctx, "decision")]
        self.assertEqual(verdict, "parked")
        self.assertEqual({r["card"] for r in rows}, {"c2"})
        self.assertTrue(any("c2" in str(a) for a in shown.call_args_list))

    def test_board_mode_loops_pass_theirs_but_a_typed_id_does_not(self):
        # Guards the contract: a card the conductor picks from the board names the card meant, so its loop gets
        # --theirs; an id a person typed does not, and still meets the renumbered-id refusal.
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        real = conduct.cli.main  # self.ade calls cli.main too: let the conduct through, record what it runs
        for typed, expect in (([], True), (["c1"], False)):
            seen = []
            fake = lambda argv, cwd=None: real(argv, cwd=cwd) if argv[:1] == ["conduct"] else seen.append(argv) or 0
            with mock.patch.object(conduct.cli, "main", fake), \
                    mock.patch("builtins.print"):
                self.ade("conduct", *typed)
            loops = [a for a in seen if a[:1] == ["loop"]]
            self.assertTrue(loops)
            self.assertEqual(all("--theirs" in a for a in loops), expect)

    def test_a_lane_whose_log_ends_in_a_traceback_is_reported_crashed_with_the_exception_line(self):
        # Guards the contract: a lane that dies on an uncaught exception says so, not just "parked".
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        ctx = config.load(str(self.repo))
        log = ctx.state / "sessions" / "lane-c1.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('Traceback (most recent call last):\n  File "x.py", line 1, in f\n    g()\nValueError: boom\n')
        self.assertEqual(conduct.crash(ctx, "c1"), "ValueError: boom")
        self.assertIsNone(conduct.crash(ctx, "c2"))  # no log, no crash
        log.write_text(log.read_text() + "parked c1: later run, no crash\n")  # logs are appended: a stale traceback is not a crash
        self.assertIsNone(conduct.crash(ctx, "c1"))

    def test_a_parked_or_held_card_is_skipped_while_its_question_is_open_then_flown_on_retry(self):
        # Guards the contract: a park files a question tied to the card, and so does --hold; the card waits while it
        # is open and flies again once the question has defaulted to 'retry' (a park) or is answered 'retry' (a hold).
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        self.ade("card", "add", "note the readme", "--path", "README.md", "--done", "line added",
                 "--hold", "waits on a person")
        ctx = config.load(str(self.repo))
        with mock.patch("builtins.print"):
            conduct._park(ctx, records.merged(ctx, "card")["c1"], None, "stuck")
        park, hold = (next(d for d in records.merged(ctx, "decision").values() if d.get("card") == c) for c in ("c1", "c2"))
        self.assertEqual((park["kind"], park["default"]), ("question", "retry"))
        self.assertIsNone(hold.get("default"))  # a hold never times out: only an answer frees it
        self.assertNotIn("landed", self.ade("conduct")[1])
        records.append(ctx, "decision", {"id": park["id"], "deadline": "2000-01-01"})
        self.ade("decide", "--answer", hold["id"], "retry")
        code, out = self.ade("conduct")
        self.assertEqual(code, 0, out)
        self.assertEqual({records.merged(ctx, "card")[c].get("status") for c in ("c1", "c2")}, {"landed"})
        # Also guards the contract: a park for an over-share provider takes the eve of its reset as the question's
        # deadline, so the card is skipped until the reset day and flies again then.
        self.ade("card", "add", "note the calc contract again", "--path", "app/calc.py", "--done", "comment added")
        ctx = config.load(str(self.repo))
        ctx.cfg["conduct"]["answerer"] = "agent"  # and the agent answerer is named, its own id too
        reset = records.today() + timedelta(days=3)
        records.append(ctx, "share", {"provider": "codex", "over": True, "until": reset.isoformat() + "T00:00:00+00:00"})
        meter = ("codex", "50%", 0.1, True)
        card = records.merged(ctx, "card")["c3"]
        with mock.patch("builtins.print"), mock.patch("ade.telemetry.meters", return_value=[meter]):
            why, eve = conduct._wake(ctx, card, None, "builder")
            conduct._park(ctx, card, None, why, eve)
        q = next(d for d in records.merged(ctx, "decision").values() if d.get("card") == "c3")
        self.assertEqual(q["deadline"], (reset - timedelta(days=1)).isoformat())
        self.assertEqual(q["answerer"], "agent")  # the field `telemetry --outcomes` counts "meant for the agent" from
        self.assertTrue(conduct.waiting(ctx, card))
        self.assertEqual(decide.state(q, reset - timedelta(days=1)), "open")
        self.assertEqual(decide.state(q, reset), "defaulted")

    def test_a_script_that_drifted_from_its_verb_fails_the_check(self):
        self.install()
        (self.repo / "tools").mkdir()
        (self.repo / "tools/go.sh").write_text("#!/bin/sh\nade loop \"$1\" && ade push\n")
        self.ade("graph")
        self.assertEqual(self.ade("graph", "--check")[0], 0)
        (self.repo / "tools/go.sh").write_text("#!/bin/sh\nade lopo \"$1\" && ade push --force\n")
        self.ade("graph")
        code, out = self.ade("graph", "--check")
        self.assertEqual(code, 2, out)
        self.assertIn("route.unbound", out)
        self.assertIn("tools/go.sh:2", out)

    def test_a_pilot_fix_inside_the_card_paths_is_committed_in_the_lane_with_a_graph_that_agrees(self):
        # Guards the contract: a pilot's uncommitted edit to the card's paths is committed in the lane after the wake
        # (the rerun reviews that tree); an edit outside the paths stays uncommitted. Whatever the fix changes in the
        # code's edges, the lane's graph file is regenerated and committed with it, so `ade graph --check` accepts the lane.
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        ctx = config.load(str(self.repo))
        lane = stages.lane_path(ctx, "c1")
        self.git("worktree", "add", "-q", "-b", "ade/c1", str(lane), "main")
        seen = []

        def pilot(ctx, role, prompt, cwd, c):
            seen.append(prompt)
            # A tracked module, so the fix adds an edge the committed graph does not yet have.
            (lane / "app/calc.py").write_text("import app\n" + (lane / "app/calc.py").read_text() + "# pilot fix\n")
            (lane / "README.md").write_text("outside the card\n")

        card = records.merged(ctx, "card")["c1"]
        before = int(self.git("-C", str(lane), "rev-list", "--count", "HEAD"))
        with mock.patch("ade.providers.call", pilot):
            conduct._wake(ctx, card, {"stage": "review", "note": "n", "next": "x"}, "pilot")
        self.assertEqual(int(self.git("-C", str(lane), "rev-list", "--count", "HEAD")), before + 1)
        self.assertIn("# pilot fix", self.git("-C", str(lane), "show", "HEAD:app/calc.py"))
        status = self.git("-C", str(lane), "status", "--porcelain")
        self.assertNotIn("app/calc.py", status)
        self.assertIn("README.md", status)
        # Also guards: the pilot's wake prompt lists every path of the card, so it knows which edits are committed for the rerun.
        for path in card["paths"]:
            self.assertIn(path, seen[0])
        self.git("-C", str(lane), "checkout", "-q", "--", "README.md")  # the edit outside the card is not the fix's
        self.assertEqual(self.git("-C", str(lane), "status", "--porcelain"), "")
        self.assertEqual(self.ade("graph", "--check", cwd=lane)[0], 0)

    def test_a_land_ask_path_lands_only_on_a_persons_answer_however_long_it_waits(self):
        # Guards the contract: a card changing a [land] ask path stops at land with an open question that has no
        # default (so it never times out), is skipped meanwhile, and lands once answered 'land'.
        self.install()
        toml = self.repo / "ade.toml"
        text = toml.read_text()
        self.assertIn("[land]\nask = []", text)
        toml.write_text(text.replace("[land]\nask = []", "[land]\nask = ['app/*']"))
        self.git("commit", "-qam", "ask")
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        ctx = config.load(str(self.repo))
        with mock.patch.dict("os.environ", {"ADE_FLIGHT": '{"hold": "c1"}'}):  # loop --hold-land rides the same env as --model
            self.assertEqual(config.load(str(self.repo)).cfg["land"]["hold"], "c1")
        self.ade("conduct")
        # A land question is no condition time clears: conduct parks the card at once, not waiting on land's wait row.
        self.assertIn("a question is open", [h for h in records.read(ctx, "decision") if h.get("card") == "c1"][-1]["text"])
        q = next(d for d in records.merged(ctx, "decision").values() if d.get("rec") == "land")
        self.assertEqual((q["default"], q["card"]), ("", "c1"))
        self.assertNotEqual(records.merged(ctx, "card")["c1"].get("status"), "landed")
        self.assertEqual(decide.state(q, records.today() + timedelta(days=7)), "open")
        self.assertTrue(conduct.waiting(ctx, records.merged(ctx, "card")["c1"]))
        # Also guards: an answer that is neither 'land' nor 'rework: TEXT' (a bare 'rework') keeps the card down and
        # a loop on it does not land it, but asks again.
        self.ade("decide", "--answer", q["id"], "rework")
        self.assertTrue(conduct.waiting(ctx, records.merged(ctx, "card")["c1"]))
        self.assertNotIn("c1 landed", self.ade("loop", "c1")[1])
        self.assertNotEqual(records.merged(ctx, "card")["c1"].get("status"), "landed")
        q = next(d for d in records.merged(ctx, "decision").values() if d.get("rec") == "land" and "answer" not in d)
        # Also guards: an answer 'rework: TEXT' sends the lane back to the builder once (journal note 'rework: TEXT'),
        # the card flies again, and land asks again; the wait's next line names the rework form beside 'land'.
        self.ade("decide", "--answer", q["id"], "rework: name the units")
        self.assertFalse(conduct.waiting(ctx, records.merged(ctx, "card")["c1"]))
        code, out = self.ade("loop", "c1")
        self.assertEqual(code, 3, out)
        self.assertIn('land (or "rework: TEXT"', out)
        stops = [(r["stage"], r["outcome"], r["note"]) for r in records.read(ctx, "journal") if r["card"] == "c1"][-4:]
        self.assertEqual([s[:2] for s in stops], [("build", "advance"), ("review", "advance"), ("test", "advance"), ("land", "wait")])
        self.assertEqual(stops[0][2], "rework: name the units")
        q = next(d for d in records.merged(ctx, "decision").values() if d.get("rec") == "land" and "answer" not in d)
        self.assertEqual(self.ade("decide", "--answer", q["id"], "land")[0], 0)
        ctx = config.load(str(self.repo))
        self.assertFalse(conduct.waiting(ctx, records.merged(ctx, "card")["c1"]))
        code, out = self.ade("land", "c1")  # also guards that the land verb calls the land stage's function
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(records.merged(ctx, "card")["c1"]["status"], "landed")

    def test_the_call_cap_stops_a_run_before_any_card_flies(self):
        # Guards the contract: [conduct] cap counts ADE's own usage rows, and a reached cap flies no card.
        self.install()
        toml = self.repo / "ade.toml"
        # ade init already wrote the template's [conduct] table, so its cap line is changed in place (a second table is invalid TOML).
        text = toml.read_text()
        self.assertIn("cap = { day = 0, week = 0 }", text)
        toml.write_text(text.replace("cap = { day = 0, week = 0 }", "cap = { day = 2, week = 0 }"))
        self.git("commit", "-qam", "cap")
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        ctx = config.load(str(self.repo))
        for _ in range(2):
            records.append(ctx, "usage", {"card": "c1", "stage": "build", "provider": "claude"})
        code, out = self.ade("conduct")
        self.assertNotIn("--- c1", out)

    def test_follow_stop_never_survives_a_day_change_and_day_does_not(self):
        # Guards the contract: --follow stops on a new day unless [conduct] follow_stop = "never".
        self.install()
        toml = self.repo / "ade.toml"
        base = toml.read_text()
        self.assertIn('follow_stop = "day"', base)
        for mode, survives in (("day", False), ("never", True)):
            toml.write_text(base.replace('follow_stop = "day"', f'follow_stop = "{mode}"'))
            real = records.today()
            tomorrow = real + timedelta(days=1)
            clock = mock.Mock(side_effect=lambda: tomorrow if clock.call_count > 3 else real)
            with mock.patch.object(records, "today", clock), mock.patch("time.sleep"):
                code, out = self.ade("conduct", "--follow", "--rounds", "3")
            self.assertEqual("3 rounds done" in out, survives, out)
            self.assertEqual("a new day began" in out, not survives, out)

    def test_a_closed_card_is_not_flown(self):
        # Guards the contract: `card close` records the reason; conduct skips the card even when it is named; list shows it closed.
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        self.assertEqual(self.ade("card", "close", "c1", "--why", "not wanted")[0], 0)
        ctx = config.load(str(self.repo))
        c = records.merged(ctx, "card")["c1"]
        self.assertEqual((c["status"], c["closed_why"]), ("closed", "not wanted"))
        self.assertTrue(c["closed_by"] and c["closed_at"])
        self.assertIn("closed", self.ade("card", "list")[1])
        self.assertNotIn("c1 landed", self.ade("conduct")[1])
        self.assertNotIn("c1 landed", self.ade("conduct", "c1")[1])
