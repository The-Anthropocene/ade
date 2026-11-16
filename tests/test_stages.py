# id: tests.test_stages
# what: the loop's lasting land hazards: a failing proof, an uncommitted fix, a cut-off landing, a moved main, new breakage, and the lane scrub
# cmd: python3 -m unittest tests/test_stages.py
# reads: tests/fixture_repo
# writes: temporary directories only
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from ade import config, providers
from ade.refuse import Refusal, refuse
from ade.stages import build, land, review, test  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase



class StagesTest(FixtureCase):
    def setUp(self):
        super().setUp()
        self.install()

    def card(self, path="app/calc.py"):
        code, out = self.ade("card", "add", "note the calc contract", "--path", path, "--done", "comment added")
        self.assertEqual(code, 0, out)
        return out.split()[0]

    def rows(self, rtype):
        rows = [json.loads(x) for x in (self.repo / ".ade/records.jsonl").read_text().splitlines()]
        return [r for r in rows if r["type"] == rtype]

    def configure(self, old, new):
        toml = self.repo / "ade.toml"
        toml.write_text(toml.read_text().replace(old, new))
        self.git("commit", "-qam", "configure")

    def reviewer_patches(self, old, new, path="app/calc.py", lf=False):
        # The reviewer answers with a real diff of the lane: replace `old` with `new` in `path`.
        real = providers.call

        def call(ctx, role, prompt, cwd, card, **kw):
            if role != "reviewer":
                return real(ctx, role, prompt, cwd, card, **kw)
            f = cwd / path
            f.write_bytes(f.read_bytes().replace(old.encode(), new.encode()))  # bytes: keep the file's line endings
            diff = subprocess.run(["git", "diff"], cwd=cwd, capture_output=True, text=True).stdout
            subprocess.run(["git", "checkout", "--", path], cwd=cwd, check=True)
            return "Found one thing.\n" + (diff.replace("\r", "") if lf else diff)
        return mock.patch("ade.providers.call", side_effect=call)

    def always(self, body):
        # An always-run check as a script outside the repo; it runs in the lane, and on main when the lane is red.
        script = self.tmp / "check.py"
        script.write_text(body)
        self.configure("always = []", f"always = ['{{python}} {script.as_posix()}']")

    def run_in(self, cwd, *argv):
        subprocess.run(argv, cwd=cwd, check=True, capture_output=True)

    def test_patch_of_ends_the_diff_at_a_closing_fence_and_only_a_first_line_of_approve_approves(self):
        # Guards the first two-lane run's slip (28 Oct): "APPROVE is not warranted" was read as an approval and landed.
        for text in ("APPROVE", "APPROVE\nlooks right", "**APPROVE**", "Approved.", "APPROVE — small and right",
                     "I cannot run commands here.\n\n**APPROVE**"):
            self.assertTrue(review.approves(text), text)
        for text in ("APPROVE is not warranted. The diff misses AGENTS.md", "I would not APPROVE", "", "diff --git a b",
                     "Note.\nAPPROVE is not warranted.", "Note.\nAPPROVE\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n"):
            self.assertFalse(review.approves(text), text)
        # Guards the contract that a fenced reply yields a bare diff: a fence line never rides into the patch.
        from ade.stages.review import patch_of
        got = patch_of("Here:\n```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\nDone\n")
        self.assertNotIn("```", got)
        self.assertTrue(got.endswith("+b\n"))
        # Also guards: a markdown file's own fence appears in a hunk as ' ```'; it must not end the diff.
        got = patch_of("--- a/x.md\n+++ b/x.md\n@@ -1,2 +1,2 @@\n ```\n-a\n+b\n```\n")
        self.assertTrue(got.endswith("-a\n+b\n"))
        self.assertIn(" ```\n", got)

    def test_review_patch_or_edit_outside_the_card_or_on_protected_config_stops(self):
        # Guards the outside-the-card rule for a reviewer's patch: it stops and prints the re-add command naming the path.
        for path, old, new in (("ade.toml", "timeout = 600", "timeout = 1"), ("README.md", "Toy", "Tiny")):
            cid = self.card()
            with self.reviewer_patches(old, new, path):
                code, out = self.ade("loop", cid)
            self.assertEqual(code, 3, out)
            self.assertIn(path, out)
        # Also guards the contract: a reviewer's direct edits meet the same outside-the-card rule as its patch.
        cid = self.card()
        real = providers.call

        def call(ctx, role, prompt, cwd, card, **kw):
            if role != "reviewer":
                return real(ctx, role, prompt, cwd, card, **kw)
            (cwd / "README.md").write_text("edited\n")
            return "APPROVE"
        with mock.patch("ade.providers.call", side_effect=call):
            code, out = self.ade("loop", cid)
        self.assertEqual(code, 3, out)
        self.assertIn("README.md", out)

    def test_red_on_main_too_is_carried_to_the_residuals_card_but_new_breakage_blocks(self):
        # Guards the residuals contract: red on main and red in the lane, but differently (the lane's extra comment), is a new failure.
        self.always("import sys\nprint('broken lines:', open('app/calc.py').read().count('#'))\nsys.exit(1)\n")
        code, out = self.ade("loop", self.card())
        self.assertEqual(code, 3, out)
        self.assertEqual(self.rows("residual"), [])
        # Also guards: the same red on main and in the lane is carried to the residuals card and the card lands.
        (self.tmp / "same.py").write_text("raise SystemExit(3)\n")
        self.configure((self.tmp / "check.py").as_posix(), (self.tmp / "same.py").as_posix())
        cid = self.card()
        code, out = self.ade("loop", cid)
        self.assertEqual(code, 0, out)
        self.assertEqual([r["card"] for r in self.rows("residual")], [cid])

    def test_a_capacity_stop_is_its_own_code_even_at_exit_0_and_the_route_retries_it(self):
        # Guards the contract: an engine at capacity never reads as a finished answer, and the route retries that code.
        from ade import route
        ctx = config.load_at(self.repo)
        env = {k: v for k, v in os.environ.items() if k != "ADE_PROVIDER"}
        for text in ("API Error: 529 {...overloaded_error...}", "Repeated 529 Overloaded errors", "Selected model is at capacity"):
            ctx.cfg["providers"]["claude"]["cmd"] = [sys.executable, "-c", f"print({text!r})"]
            with mock.patch.dict("os.environ", env, clear=True), self.assertRaises(Refusal) as cm:
                providers.call(ctx, "builder", "go", self.repo, {"id": "c1"})
            self.assertIn("provider.capacity", repr(cm.exception.args) + str(vars(cm.exception)))
        # A plan's limit answer exits 1 beside a stderr warning: still capacity, with the reset time kept.
        limit = "import sys; sys.stderr.write('warning: x\\n'); print('You have hit your limit - resets 3pm'); sys.exit(1)"
        ctx.cfg["providers"]["claude"]["cmd"] = [sys.executable, "-c", limit]
        with mock.patch.dict("os.environ", env, clear=True), self.assertRaises(Refusal) as cm:
            providers.call(ctx, "builder", "go", self.repo, {"id": "c1"})
        self.assertIn("provider.capacity", repr(cm.exception.args) + str(vars(cm.exception)))
        self.assertIn("resets 3pm", repr(cm.exception.args) + str(vars(cm.exception)))
        # Method overloading in a closing line is an answer, not a capacity stop.
        ctx.cfg["providers"]["claude"]["cmd"] = [sys.executable, "-c", "print('APPROVE: the overloaded constructor is fine')"]
        with mock.patch.dict("os.environ", env, clear=True):
            providers.call(ctx, "builder", "go", self.repo, {"id": "c1"})
        # A short normal answer that merely mentions a usage limit is an answer too.
        ctx.cfg["providers"]["claude"]["cmd"] = [sys.executable, "-c", "print('Added the usage limit check.')"]
        with mock.patch.dict("os.environ", env, clear=True):
            providers.call(ctx, "builder", "go", self.repo, {"id": "c1"})
        # A long parsed answer that quotes the error while discussing it is an answer too, even in its last line.
        quote = json.dumps({"result": "line one\nline two\nline three\nAPPROVE: the check looks for overloaded_error"})
        ctx.cfg["providers"]["claude"]["cmd"] = [sys.executable, "-c", "print(" + repr(quote) + ")"]
        with mock.patch.dict("os.environ", env, clear=True):
            providers.call(ctx, "builder", "go", self.repo, {"id": "c1"})
        row = route.pick(route.load()["stop"], "build", "needs-judgement", "provider.capacity")
        self.assertEqual(row["then"], "retry")
        # A credit-exhausted claude answer is its own stop the route never retries, whatever stderr says first.
        code = "import sys;print('Credit balance is too low');print('workspace trust warning',file=sys.stderr)"
        ctx.cfg["providers"]["claude"]["cmd"] = [sys.executable, "-c", code]
        with mock.patch.dict("os.environ", env, clear=True), self.assertRaises(Refusal) as cm:
            providers.call(ctx, "builder", "go", self.repo, {"id": "c1"})
        self.assertEqual(cm.exception.code, "provider.credit")
        self.assertNotEqual(route.pick(route.load()["stop"], "build", "needs-judgement", "provider.credit")["then"], "retry")

    def test_a_providers_env_drops_scrubbed_variables_and_alone_gets_its_secret(self):
        # Guards the scrub: a host Claude Code session's variables would make a child claude act as that session (card 5, 27 Oct).
        spec = {"scrub_env": ["CLAUDECODE", "CLAUDE_CODE_*"]}
        with mock.patch.dict("os.environ", {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "HOME_KEPT": "y"}):
            env = providers._env(spec)
        self.assertFalse({"CLAUDECODE", "CLAUDE_CODE_SESSION_ID"} & set(env))
        self.assertEqual(env["HOME_KEPT"], "y")
        # Also guards: a host key or token never reaches a lane; only the engine's own auth variable does.
        with mock.patch.dict("os.environ", {"GH_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s", "ANTHROPIC_API_KEY": "a",
                                     "openai_api_key": "o", "ANTHROPIC_AUTH_TOKEN": "p"}):
            self.assertFalse({"GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "openai_api_key"} & set(providers._env(spec, "claude")))
            self.assertEqual(providers._env(spec, "claude")["ANTHROPIC_API_KEY"], "a")
            self.assertEqual(providers._env(spec, "claude")["ANTHROPIC_AUTH_TOKEN"], "p")
            self.assertFalse({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "openai_api_key"} & set(providers._env(spec, "codex")))
        # Also guards pass_env: a named variable survives the scrub; a key, token or plan-login name refuses at load.
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h", "CLAUDE_CODE_SESSION_ID": "x"}):
            env = providers._env({**spec, "pass_env": ["CLAUDE_CODE_PROMPT_CACHE_TTL"]})
        self.assertEqual((env["CLAUDE_CODE_PROMPT_CACHE_TTL"], "CLAUDE_CODE_SESSION_ID" in env), ("1h", False))
        cfg = {"guards": {"secrets": ["*.pem"]}, "providers": {"claude": {"pass_env": ["CLAUDE_CODE_PROMPT_CACHE_TTL"]}}}
        providers.check_pass_env(cfg)
        for bad in ("ANTHROPIC_API_KEY", "GH_TOKEN", "my_secret", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_ENTRYPOINT", "X.PEM"):
            cfg["providers"]["claude"]["pass_env"] = [bad]
            with self.assertRaises(Refusal):
                providers.check_pass_env(cfg)
        # Also guards the invariant: a key file's value lands in the provider's environment, either file shape, never ade's own.
        with mock.patch.dict("os.environ"):
            os.environ.pop("OPENROUTER_API_KEY", None)
            for body, want in (("openrouter_key=sk-test\n", "sk-test"), ("sk-test", "sk-test"), ("c2stdGVzdA==", "c2stdGVzdA==")):
                key = self.repo.parent / "key.txt"
                key.write_text(body)
                env = providers._env({"secrets": {"OPENROUTER_API_KEY": str(key)}})
                self.assertEqual(env["OPENROUTER_API_KEY"], want)
                self.assertNotIn("OPENROUTER_API_KEY", os.environ)
        with self.assertRaises(Refusal):
            providers._env({"secrets": {"OPENROUTER_API_KEY": str(self.repo.parent / "missing.txt")}})

    def test_a_lane_is_never_on_trunk_nor_made_without_a_local_trunk(self):
        # Guards the sibling of B2: an existing lane sitting on trunk would land its work on trunk itself; the stage
        # refuses and the cure names the card's own branch.
        from ade.stages import card, lane, lane_path
        cid = self.card()
        ctx = config.load(self.repo)
        c = card(ctx, cid)  # the whole card: making a lane reads its paths and words
        lane.run(ctx, c)
        self.git("checkout", "-q", "--detach")  # the main checkout lets go of main so the lane may take it
        self.run_in(lane_path(ctx, cid), "git", "checkout", "-q", "main")
        with self.assertRaises(Refusal) as r:
            lane.run(ctx, c)
        self.assertEqual(r.exception.code, "lane.refused")
        self.assertIn("ade/" + cid, r.exception.cure)
        # Also guards B2: with no local trunk branch git would put the lane on trunk itself; the stage refuses and makes none.
        self.run_in(lane_path(ctx, cid), "git", "checkout", "-q", "--detach")  # so main can be deleted
        cid = self.card()
        self.git("checkout", "-q", "-b", "feature")
        self.git("branch", "-D", "main")
        ctx = config.load(self.repo)
        with self.assertRaises(Refusal) as r:
            lane.run(ctx, {"id": cid})
        self.assertEqual(r.exception.code, "lane.refused")
        self.assertFalse(lane_path(ctx, cid).exists())
        # Guards the sandbox cure: a lane folder the engine cannot write is a refusal naming the cures, not a crash.
        self.git("checkout", "-q", "-b", "main")  # the trunk deleted above comes back, so the stage reaches the folder
        for fails in ("mkdir", "write_text"):  # a failing mkdir, and an existing folder whose probe write fails
            with mock.patch(f"pathlib.Path.{fails}", side_effect=PermissionError(1, "Operation not permitted")):
                with self.assertRaises(Refusal) as r:
                    lane.run(ctx, {"id": cid})
            self.assertEqual(r.exception.code, "lane.work_unwritable")
            self.assertIn("writable_roots", r.exception.cure)
        # Also guards the Codex sandbox refusal: it comes before the lane folder is made.
        with mock.patch.dict("os.environ", {"CODEX_SANDBOX": "seatbelt"}):
            with self.assertRaises(Refusal) as r:
                lane.run(ctx, {"id": cid})
        self.assertEqual(r.exception.code, "lane.sandboxed")
        self.assertIn("danger-full-access", r.exception.cure)
        self.assertFalse(lane_path(ctx, cid).exists())

    def test_review_patch_lf_applies_to_a_crlf_file(self):
        # Guards a Windows hazard: models write LF diffs, a checkout can hold CRLF files, and the LF patch still applies.
        f = self.repo / "app/calc.py"
        f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        if self.git("status", "--porcelain"):  # a Windows checkout may already hold it as CRLF
            self.git("commit", "-qam", "crlf")
        cid = self.card()
        with self.reviewer_patches("return a + b", "return a + b  # reviewed", lf=True):
            code, out = self.ade("loop", cid)
        self.assertEqual(code, 0, out)
        self.assertIn("patch applied", out)

    def test_a_lane_changing_its_tests_table_parks_before_any_test_runs(self):
        # Guards verifier integrity: a lane that rewrites how it is tested is refused (tests.protected) before any test runs.
        from ade.stages import card, lane, lane_path
        cid = self.card()
        ctx = config.load(self.repo)
        c = card(ctx, cid)
        lane.run(ctx, c)
        lp = lane_path(ctx, cid)
        toml = lp / "ade.toml"
        toml.write_text(toml.read_text().replace("timeout = 600", "timeout = 601", 1))
        self.run_in(lp, "git", "commit", "-qam", "edit config")
        with mock.patch("ade.stages.test._run") as ran:
            with self.assertRaises(Refusal) as r:
                test.run(ctx, c)
        self.assertEqual(r.exception.code, "tests.protected")
        ran.assert_not_called()

    def test_land_stops_on_protected_changes_unless_they_are_exactly_what_init_renders(self):
        # Guards the contract: land's own check (the only one an engine with no edit hook meets) stops a lane's change
        # to a [protected] path unless the file is what `ade init` renders; a widened allow list is not waved through.
        from ade import install
        from ade.stages import card, lane, lane_path
        (self.repo / "AGENTS.md").write_text("stale\n")  # main's generated file is behind what init renders
        self.git("commit", "-qam", "stale agents")
        for rel, handmade in (("AGENTS.md", False), ("ade.toml", True), (".claude/settings.json", True)):
            cid = self.card()
            ctx = config.load(self.repo)
            c = card(ctx, cid)
            lane.run(ctx, c)
            lp = lane_path(ctx, cid)
            f = lp / rel
            render = dict(install.plan(config.load_at(lp)))[rel]
            if rel == ".claude/settings.json":
                s = json.loads(render)
                s.setdefault("permissions", {}).setdefault("allow", []).append("Bash(curl *)")
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(s, indent=2) + "\n")
            else:
                f.write_text(f.read_text() + "# hand edit\n" if handmade else render)
            self.run_in(lp, "git", "add", "-A", rel)
            self.run_in(lp, "git", "commit", "-qm", "protected change")
            self.assertEqual(land.protected_changes(ctx, c, lp), [rel] if handmade else [])

    def test_land_rechecks_on_main_only_when_main_moved_under_the_lane(self):
        before = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-qc", "ade/c1")
        calc = self.repo / "app/calc.py"
        calc.write_text(calc.read_text() + "# lane edit\n")
        self.git("commit", "-qam", "lane edit")
        self.git("switch", "-q", "main")
        ctx = config.load(str(self.repo))
        self.git("merge", "-q", "--no-ff", "-m", "land", "ade/c1")
        self.assertIsNone(land.backstop(ctx, {"id": "c1"}, "-", before))  # main held still: the lane's own test stands
        self.git("reset", "-q", "--hard", before)
        # Main moves under the lane and now fails a test the lane's change reaches: the merged main is red.
        test_file = self.repo / "tests/test_calc.py"
        test_file.write_text(test_file.read_text().replace("5)", "6)"))
        self.git("commit", "-qam", "main moves")
        moved = self.git("rev-parse", "HEAD").strip()
        self.git("merge", "-q", "--no-ff", "-m", "land", "ade/c1")
        self.assertTrue(land.backstop(ctx, {"id": "c1"}, "-", moved))

    def test_a_card_proof_is_run_by_land_and_a_failing_one_leaves_main_where_it_was(self):
        # Guards the contract: the outcome's proof rc and output hash come from the child, and a non-zero rc stops the landing.
        ok = "{python} -c \"print('shown')\""
        bad = "{python} -c \"raise SystemExit(2)\""
        before = self.git("rev-parse", "HEAD").strip()
        code, out = self.ade("card", "add", "t", "--path", "app/calc.py", "--done", "d", "--proof", bad)
        cid = out.split()[0]
        with self.reviewer_patches("return a + b", "return a + b  # reviewed"):
            code, out = self.ade("loop", cid)
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        code, out = self.ade("card", "add", "u", "--path", "app/calc.py", "--done", "d", "--proof", ok)
        cid = out.split()[0]
        with self.reviewer_patches("return a + b", "return a + b  # reviewed"):
            code, out = self.ade("loop", cid)
        self.assertEqual(code, 0, out)
        (proof,) = [r for r in self.rows("outcome") if r["card"] == cid][0]["proofs"]
        self.assertEqual((proof["cmd"], proof["rc"]), (ok, 0))
        self.assertRegex(proof["out_sha256"], r"[0-9a-f]{64}")
        # Also guards: a merge that leaves any budget row over is put back unpushed and named; within budget it lands (above).
        before = self.git("rev-parse", "HEAD").strip()
        code, out = self.ade("card", "add", "w", "--path", "app/calc.py", "--done", "d")
        with mock.patch("ade.budget.measure", return_value=[("core lines", 9, 5)]), \
                self.reviewer_patches("return a + b", "return a + b  # again"):
            code, out = self.ade("loop", out.split()[0])
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)

    def test_a_landing_cut_off_after_its_merge_finishes_without_a_second_merge(self):
        # Guards the contract that a crash between land's merge and its records leaves nothing to redo but the records.
        ok = "{python} -c \"print('shown')\""
        code, out = self.ade("card", "add", "t", "--path", "app/calc.py", "--done", "d", "--proof", ok)
        cid = out.split()[0]
        # budget.measure is shared with build's cap line: stub that so the cut-off lands after the merge, not in build.
        with self.reviewer_patches("return a + b", "return a + b  # reviewed"), \
                mock.patch("ade.stages.build.cap_line", return_value=""), \
                mock.patch("ade.stages.land.budget.measure", side_effect=RuntimeError("cut off")):
            try:
                self.ade("loop", cid)
            except RuntimeError:
                pass
        merges = self.git("log", "--merges", "--first-parent", "--format=%H", "main").split()
        self.assertEqual(len(merges), 1)
        self.assertEqual([r for r in self.rows("card") if r.get("status") == "landed"], [])
        code, out = self.ade("loop", cid)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git("log", "--merges", "--first-parent", "--format=%H", "main").split(), merges)
        landed = [r for r in self.rows("card") if r.get("status") == "landed"]
        self.assertEqual([r["commit"] for r in landed], merges)
        # Also guards that an interrupted landing cannot bypass a proof: the rerun records the proof's rc.
        (proof,) = [r for r in self.rows("outcome") if r["card"] == cid][0]["proofs"]
        self.assertEqual((proof["cmd"], proof["rc"]), (ok, 0))

    def test_landing_beside_uncommitted_edits_leaves_them_alone_and_waits_only_on_shared_paths(self):
        # Guards the person's work: a card lands around unrelated edits (tracked or untracked) without touching a byte,
        # and an edit to a path the card changes waits, naming that path.
        cid = self.card()
        readme, scratch, calc = self.repo / "README.md", self.repo / "scratch.txt", self.repo / "app/calc.py"
        readme.write_bytes(readme.read_bytes() + b"my edit\r\n")
        scratch.write_bytes(b"untracked\x00bytes")
        kept = (readme.read_bytes(), scratch.read_bytes())
        calc.write_text(calc.read_text() + "# mine\n")
        code, out = self.ade("loop", cid)
        self.assertNotEqual(code, 0, out)
        self.assertIn("app/calc.py", out)
        self.git("checkout", "--", "app/calc.py")
        code, out = self.ade("loop", cid)
        self.assertEqual(code, 0, out)
        self.assertEqual((readme.read_bytes(), scratch.read_bytes()), kept)

    def test_an_uncommitted_fix_in_the_lane_stops_the_landing_instead_of_being_discarded(self):
        # Guards a fix that passed in the lane but was never committed: landing merges commits only, then removes the
        # lane, so the landing stops and names the file rather than losing it.
        cid = self.card()
        calc = self.repo / "app/calc.py"
        calc.write_text(calc.read_text() + "# mine\n")  # main shares a path with the lane: the first pass waits
        self.ade("loop", cid)
        self.git("checkout", "--", "app/calc.py")
        lp = Path(self.rows("journal")[0]["note"].split()[-1])
        (lp / "README.md").write_text("a fix nobody committed\n")
        code, out = self.ade("loop", cid)
        self.assertNotEqual(code, 0, out)
        self.assertIn("README.md", out)
        self.assertEqual((lp / "README.md").read_text(), "a fix nobody committed\n")
        self.git("-C", str(lp), "checkout", "--", "README.md")
        (lp / "brand_new.py").write_text("x = 1\n")  # a new file is a loose edit too
        code, out = self.ade("loop", cid)
        self.assertNotEqual(code, 0, out)
        self.assertIn("brand_new.py", out)

    def test_a_rehearsal_never_pushes_on_landing(self):
        self.git("remote", "add", "origin", str(self.tmp / "remote.git"))
        self.assertIn("rehearsal", land.ship(config.load(str(self.repo))))  # helpers run every test with the fake provider

    def test_a_refusal_that_land_only_prints_is_still_counted(self):
        # Guards the contract that every caught Refusal leaves a "refusal" row, so `ade budget --refusals` sees it.
        ctx = config.load(str(self.repo))
        with mock.patch("ade.push.push", side_effect=lambda *a, **k: refuse("push.failed", "no remote", "add one")):
            land.open_pr(ctx, {"id": "c1", "title": "t"})
        self.assertIn("push.failed", [r["code"] for r in self.rows("refusal")])
