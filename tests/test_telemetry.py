# id: tests.test_telemetry
# what: a provider's usage report becomes one usage row; meters come from the engines' own reports, say stale or unknown, never 0; headless spend is inferred from the jump
# cmd: python3 -m unittest tests/test_telemetry.py
# reads: tests/fixture_repo
# writes: temporary directories only
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ade import config, providers, records, telemetry
from ade.refuse import Refusal
from ade.verbs import telemetry as telemetry_verb  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase

CLAUDE = {"type": "result", "result": "APPROVE\nlooks right", "num_turns": 3, "total_cost_usd": 0.0412,
          "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": 300}}
CODEX = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "APPROVE"}},
    {"type": "token_count", "rate_limits": {"primary": {"used_percent": 40.0, "window_minutes": 300},
                                            "secondary": {"used_percent": 12.5, "window_minutes": 10080,
                                                          "resets_in_seconds": 86400}}},
    {"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 20}}])

def engine_popen(is_engine, answer, stderr=""):
    """A stand-in for subprocess.Popen (what providers.call starts the engine with): an argv that is_engine gets a
    finished process whose returncode is answer(argv, kwargs) and that wrote stderr; anything else (records' git
    calls) runs for real."""
    real = subprocess.Popen

    def popen(argv, *a, **k):
        if not is_engine(argv):
            return real(argv, *a, **k)
        p = mock.Mock(pid=2**31 - 1, returncode=answer(argv, k))
        p.communicate.return_value = (None, stderr)
        return p
    return mock.Mock(side_effect=popen)


class TelemetryTest(FixtureCase):
    def test_each_provider_report_parses_and_silence_is_not_zero(self):
        # Guards: a provider's report parses to one usage row's fields, and a field it never reported is absent, never a made-up 0.
        text, f = telemetry.parse(json.dumps(CLAUDE))
        self.assertEqual(text, "APPROVE\nlooks right")
        self.assertEqual(f, {"tokens_in": 1200, "tokens_out": 80, "cache_read": 9000, "cache_write": 300,
                             "cost": 0.0412, "turns": 3})
        text, f = telemetry.parse(CODEX)
        self.assertEqual(text, "APPROVE")
        self.assertEqual((f["tokens_in"], f["cache_read"], f["tokens_out"], f["week_used"]), (500, 100, 20, 0.125))
        self.assertNotIn("cost", f)  # codex reports no cost; none is recorded rather than a made-up 0
        self.assertEqual(telemetry.parse("APPROVE\nplain text\n"), ("APPROVE\nplain text\n", {}))

    def test_a_real_provider_call_writes_its_usage_row(self):
        # Guards the usage-row contract: every engine call, passed or failed, leaves one row and its lane-log section.
        self.install()
        ctx = config.load(str(self.repo))
        rc = [0]

        def answer(argv, k):
            k["stdout"].write(json.dumps(CLAUDE).encode())  # the provider writes into the lane log
            return rc[0]
        # Only the provider's process is stood in for; git (records) still runs for real.
        fake = engine_popen(lambda argv: str(argv[0]).endswith("claude"), answer, "workspace has not been trusted\n")
        with mock.patch.dict("os.environ", {"ADE_PROVIDER": "claude"}), \
                mock.patch("ade.providers.shutil.which", return_value="/bin/claude"), \
                mock.patch("subprocess.Popen", fake) as run:
            text = providers.call(ctx, "reviewer", "review this", str(self.repo), {"id": "c1"})
        self.assertEqual(text, "APPROVE\nlooks right")
        self.assertIn("--output-format", next(c[0][0] for c in run.call_args_list if str(c[0][0][0]).endswith("claude")))
        row = records.read(ctx, "usage")[-1]
        self.assertEqual((row["card"], row["stage"], row["provider"], row["model"], row["cost"]),
                         ("c1", "review", "claude", "sonnet", 0.0412))
        self.assertIn("effort", row)  # always written (null when none passed), beside the alias in "model"
        # A call that fails after spending still leaves its row, marked failed, before the refusal.
        rc[0] = 1
        with mock.patch.dict("os.environ", {"ADE_PROVIDER": "claude"}), \
                mock.patch("ade.providers.shutil.which", return_value="/bin/claude"), mock.patch("subprocess.Popen", fake):
            with self.assertRaises(Refusal):
                providers.call(ctx, "reviewer", "review this", str(self.repo), {"id": "c1"})
        self.assertEqual(records.read(ctx, "usage")[-1]["failed"], 1)
        log = (self.repo / ".ade/lanes/c1.log").read_text()
        self.assertEqual(log.count("--- reviewer via claude ---"), 2)  # both calls, live in the lane log
        # Also guards: a warning the engine writes to stderr is kept in the lane log after a marker, never dropped.
        self.assertIn("--- stderr ---\nworkspace has not been trusted", log)
        # Also guards the tripwire contract: key + 1h writes -> billing_suspect and a stderr line; no key -> neither.
        report = {**CLAUDE, "usage": {**CLAUDE["usage"], "cache_creation": {"ephemeral_1h_input_tokens": 300}}}
        self.assertEqual(telemetry.parse(json.dumps(report))[1]["cache_write_1h"], 300)
        keyfile = self.repo / "key"
        keyfile.write_text("sk-test")
        fake = engine_popen(lambda argv: str(argv[0]).endswith("claude"),
                            lambda argv, k: k["stdout"].write(json.dumps(report).encode()) and 0)
        # A lane passed CLAUDE_CODE_PROMPT_CACHE_TTL=1h (pass_env) writes 1h cache on purpose: not flagged.
        key = {"ANTHROPIC_API_KEY": str(keyfile)}
        for secrets, ttl, suspect in ((key, {}, True), ({}, {}, False), (key, {"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}, False)):
            ctx.cfg["providers"]["claude"]["secrets"] = secrets
            ctx.cfg["providers"]["claude"]["pass_env"] = list(ttl)  # unpassed, a host's own TTL is scrubbed
            with mock.patch.dict("os.environ", {"ADE_PROVIDER": "claude", **ttl}), \
                    mock.patch("ade.providers.shutil.which", return_value="/bin/claude"), \
                    mock.patch("subprocess.Popen", fake), mock.patch("sys.stderr") as err:
                providers.call(ctx, "reviewer", "review this", str(self.repo), {"id": "c1"})
            row = records.read(ctx, "usage")[-1]
            self.assertEqual(row.get("billing_suspect", False), suspect)
            self.assertEqual("1-hour cache" in "".join(str(c) for c in err.write.call_args_list), suspect)

    def test_lane_mode_reaches_each_engines_argv(self):
        # Guards the contract: [permissions] lane_mode sets codex's sandbox flags (its modes table) and claude's
        # --permission-mode, so a lane's rights never hang on the user's own engine config.
        self.install()
        ctx = config.load(str(self.repo))
        sent = []
        # records' own git calls (the usage row restores the records branch) run for real
        fake = engine_popen(lambda argv: argv[0] != "git", lambda argv, k: sent.append(list(argv)) or 0)
        for mode, codex_flags in (("acceptEdits", ['default_permissions="ade"']),
                                  ("bypassPermissions", ['default_permissions="ade"'])):
            ctx.cfg["permissions"]["lane_mode"] = mode
            ctx.cfg["guards"]["secrets"] = [*ctx.cfg["guards"]["secrets"], "/abs/secret/*"]
            for engine in ("codex", "claude"):
                sent.clear()
                with mock.patch.dict("os.environ", {"ADE_PROVIDER": engine}), \
                        mock.patch("ade.providers.shutil.which", return_value="/bin/" + engine), \
                        mock.patch("subprocess.Popen", fake):
                    providers.call(ctx, "reviewer", "review this", str(self.repo), {"id": "c1"})
                argv = next(a for a in sent if a[0].endswith(engine))
                if engine == "codex":
                    i = argv.index(codex_flags[0])
                    self.assertEqual(argv[i:i + len(codex_flags)], codex_flags)
                    self.assertNotIn("{mode}", argv)
                    # a lane is a fresh seat: codex's own memory store stays off
                    self.assertEqual(argv[argv.index("memories.use_memories=false") - 1], "-c")
                    # every mode: secrets denied under the lane (-s would override the profile), network on, git dirs writable
                    joined = " ".join(argv)
                    self.assertNotIn("--sandbox", argv)
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
                    self.assertIn(f'"{self.repo.as_posix()}/**/.env" = "deny"', joined)
                    self.assertIn('"~/**/.env" = "deny"', joined)
                    self.assertNotIn('"/**/', joined)  # codex refuses a deny glob rooted at "/"
                    self.assertIn('"/abs/secret/*" = "deny"', joined)
                    self.assertIn("permissions.ade.network.enabled=true", argv)
                    self.assertIn('/.git" = "write"', joined)
                else:
                    self.assertEqual(argv[argv.index("--permission-mode") + 1], mode)
                    # Guards the contract: a claude lane gets its rules and read sandbox by --settings, not by the checkout.
                    with mock.patch("ade.providers.sys.platform", "darwin"):
                        sent.clear()
                        with mock.patch.dict("os.environ", {"ADE_PROVIDER": engine}), \
                                mock.patch("ade.providers.shutil.which", return_value="/bin/claude"), \
                                mock.patch("subprocess.Popen", fake):
                            providers.call(ctx, "reviewer", "review this", str(self.repo), {"id": "c1"})
                    argv = next(a for a in sent if a[0].endswith(engine))
                    s = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
                    self.assertIn("Bash(ade *)", s["permissions"]["allow"])
                    self.assertIn("/**/.env", s["sandbox"]["filesystem"]["denyRead"])
                    self.assertIs(s["sandbox"]["allowUnsandboxedCommands"], False)
                    # Guards the contract: verbs in a sandboxed lane can write ADE state, and no secret read-deny is re-opened.
                    allow = s["sandbox"]["filesystem"]["allowWrite"]
                    self.assertIn(str(ctx.state), allow)
                    self.assertEqual(allow, [str(ctx.state)])  # no git ref or object path: lanes never write git
                    self.assertFalse(set(allow) & set(s["sandbox"]["filesystem"]["denyRead"]))
        # Also guards the contract: Claude's sandbox lets only an exact-path allowRead override a glob denyRead, so secret_ok
        # matches under the lane cwd are listed by absolute path, and a secret that is not secret_ok never is.
        ctx = config.load(str(self.repo))
        ctx.cfg["guards"]["secrets"] = [".atlas/keys/*"]
        ctx.cfg["guards"]["secret_ok"] = [".env.example"]
        with tempfile.TemporaryDirectory() as d:
            for n in (".env", ".env.example"):
                (Path(d) / n).write_text("x", encoding="utf-8")
            with mock.patch("ade.providers.sys.platform", "darwin"):
                s = json.loads(providers._lane_settings(ctx, "c1", d).read_text(encoding="utf-8"))
            fs = s["sandbox"]["filesystem"]
            self.assertIn(str(Path(d) / ".env.example"), fs["allowRead"])
            self.assertNotIn(str(Path(d) / ".env"), fs["allowRead"])
        # Also guards: secret_ok stays readable in the same glob form denyRead uses.
        self.assertIn("/**/.env.example", fs["allowRead"])
        # Also guards: --settings carries the Read and Grep denies init writes for [guards] secrets, since an
        # untrusted lane checkout ignores the committed .claude/settings.json.
        self.assertIn("Read(~/.atlas/keys/*)", s["permissions"]["deny"])
        self.assertIn("Grep(~/.atlas/keys/*)", s["permissions"]["deny"])
        # Also guards the property: in a git checkout, files under a .gitignore'd folder (venv, build, caches) are never listed.
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            (Path(d) / ".gitignore").write_text("venv/\n", encoding="utf-8")
            (Path(d) / "venv").mkdir()
            for n in (".env.example", "venv/.env.example"):
                (Path(d) / n).write_text("x", encoding="utf-8")
            got = providers._secret_ok_files(d, [".env.example"])
            self.assertEqual(got, [os.path.join(d, ".env.example")])

    def test_no_engine_child_outlives_a_call_that_exits_times_out_or_stalls(self):
        # Guards the property: no descendant of an engine outlives its call, however it ends (it would hold the lane git
        # lock; an npm wrapper's native child): the POSIX process group, or on Windows the engine's Job Object, ends its
        # sleeping grandchild. Also guards the contract: a hung engine ends as provider.failed (and a failed usage row),
        # never a TimeoutExpired, and one whose lane log stops growing for `stall` seconds is stopped long before its timeout.
        self.install()
        ctx = config.load(str(self.repo))
        pidfile = self.repo / "grandchild.pid"
        spawn = ("import subprocess, sys, time\n"
                 "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
                 "                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                 "open(sys.argv[1], 'w').write(str(p.pid))\n")
        cases = (("exits", "", {"timeout": 30}, None),
                 ("times out", "time.sleep(60)\n", {"timeout": 2}, "timeout"),
                 ("stalls", "print('once', flush=True)\ntime.sleep(60)\n", {"timeout": 60, "stall": 2}, "silent for 2s"))

        def alive(pid):
            if os.name == "nt":
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
                return str(pid) in out
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        for name, tail, bounds, cause in cases:
            pidfile.unlink(missing_ok=True)
            ctx.cfg["providers"]["claude"].update(cmd=[sys.executable, "-c", spawn + tail, str(pidfile)], **bounds)
            began = time.monotonic()
            with mock.patch.dict("os.environ", {"ADE_PROVIDER": "claude"}):
                if cause:
                    with self.assertRaises(Refusal) as stop:
                        providers.call(ctx, "reviewer", "review this", str(self.repo), {"id": "c1"})
                    self.assertEqual(stop.exception.code, "provider.failed", name)
                    self.assertIn(cause, stop.exception.cause, name)
                    self.assertTrue(records.read(ctx, "usage")[-1]["failed"], name)
                    self.assertLess(time.monotonic() - began, 30, name)
                else:
                    providers.call(ctx, "reviewer", "review this", str(self.repo), {"id": "c1"})
            pid = int(pidfile.read_text())
            for _ in range(50):
                if not alive(pid):
                    break
                time.sleep(0.1)
            else:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, 9)
                self.fail(f"{name}: grandchild still running")

    def test_week_meters_read_unknown_until_reported_and_flag_over_share(self):
        self.install()
        ctx = config.load(str(self.repo))
        # Never given a window is not a reading at all; it says so rather than "unknown" or 0.
        self.assertEqual(telemetry.meters(ctx, ["codex"]), [("codex", "not reported", 1.0, False)])
        self.assertEqual(telemetry.meters(ctx, ["fake"]), [])  # the fake has no week
        ctx.cfg["usage"]["share"]["codex"] = 0.1
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        telemetry.record(ctx, "c1", "build", "builder", "codex", "gpt-6-sol", 1.0, {"week_used": 0.125, "week_resets": soon})
        self.assertEqual(telemetry.meters(ctx, ["codex"]), [("codex", "12%", 0.1, True)])
        gone = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        telemetry.record(ctx, "c1", "build", "builder", "codex", "gpt-6-sol", 1.0, {"week_used": 0.9, "week_resets": gone})
        self.assertEqual(telemetry.meter(ctx, "codex"), None)  # that window has reset: unknown, not 90%
        # A reset with no zone is read as UTC; one nobody can read makes the meter unknown, never a crash.
        later = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None).isoformat(timespec="seconds")
        self.assertEqual(telemetry.parse(json.dumps({"rate_limits": {"weekly": {
            "used_percent": 30, "window_minutes": 10080, "resets_at": later}}}))[1]["week_used"], 0.3)
        telemetry.record(ctx, "c1", "build", "builder", "codex", "gpt-6-sol", 1.0, {"week_used": 0.3, "week_resets": later})
        self.assertEqual(telemetry.meter(ctx, "codex"), 0.3)
        telemetry.record(ctx, "c1", "build", "builder", "codex", "gpt-6-sol", 1.0, {"week_used": 0.3, "week_resets": "soon"})
        self.assertEqual(telemetry.meter(ctx, "codex"), None)
        # Guards the owner's 27/10/2026 rule: the one meter fact that leaves the machine, the share flag, is a yes/no
        # with its reset, and moves only on change (the last row above said over).
        seen = len(records.read(ctx, "share"))
        for used in (0.05, 0.125, 0.125, 0.5, 0.05):
            telemetry.record(ctx, "c1", "build", "builder", "codex", "gpt-6-sol", 1.0, {"week_used": used, "week_resets": soon})
            telemetry.meters(ctx, ["codex"])
        rows = records.read(ctx, "share")
        self.assertEqual([(r["over"], r["until"]) for r in rows[seen:]], [(False, None), (True, soon), (False, None)])
        for r in rows:
            self.assertEqual(set(r) - {"type", "at"}, {"provider", "over", "until"})

    def test_real_sources_feed_the_meters_and_headless_spend_is_inferred_from_the_jump(self):
        self.install()
        ctx = config.load(str(self.repo))
        now = datetime.now(timezone.utc)
        iso = lambda t: t.isoformat(timespec="seconds")  # noqa: E731
        later = iso(now + timedelta(days=3))
        # Codex: the newest token_count event in its own session files is the reading, and its age matters.
        rollout = self.tmp / "codex/sessions/2026/10/27/rollout-x.jsonl"
        rollout.parent.mkdir(parents=True)

        def codex(used, at):
            rollout.write_text(json.dumps({"timestamp": iso(at), "type": "event_msg", "payload": {
                "type": "token_count", "rate_limits": {"primary": {
                    "used_percent": used, "window_minutes": 10080, "resets_at": later}}}}) + "\n")
        ctx.cfg["usage"]["share"]["codex"] = 0.5
        codex(40, now)
        self.assertEqual(telemetry.meters(ctx, ["codex"]), [("codex", "40%", 0.5, False)])
        codex(60, now + timedelta(seconds=1))
        self.assertTrue(telemetry.meters(ctx, ["codex"])[0][3])
        self.assertEqual(telemetry.reading(ctx, "codex", now + telemetry.STALE + timedelta(minutes=5))[0], "stale")
        # Claude: the status line hands ade its own reading, with the interactive session's dollars.
        line = {"session_id": "s1", "cost": {"total_cost_usd": 1.0},
                "rate_limits": {"seven_day": {"used_percentage": 10, "resets_at": later}}}
        self.ade("status", "--line", stdin=json.dumps(line))
        self.assertEqual(telemetry.meters(ctx, ["claude"])[0][1], "10%")
        self.assertEqual(records.read(ctx, "meter"), [])  # kept once per machine, so every repo reads the same week
        # Guards one meter file per machine: another ADE_WORK reads the same readings, and an older split file is still taken in.
        with mock.patch.dict(os.environ, {"ADE_WORK": str(self.tmp / "other")}):
            self.assertEqual(telemetry.meters(ctx, ["claude"])[0][1], "10%")
        # One headless call inside a window with no interactive spend owns the whole jump, exactly.
        a = telemetry.readings(ctx)[-1]
        start = datetime.fromisoformat(a["seen"])
        telemetry.record(ctx, "c1", "build", "builder", "claude", "sonnet", 0.0,
                         {"cost": 0.3, "at": iso(start + timedelta(minutes=5))})
        telemetry.note(ctx, "claude", "status line", line["rate_limits"] | {"seven_day": {
            "used_percentage": 14, "resets_at": later}}, start + timedelta(minutes=10), "s1", 1.0)
        [e] = telemetry.headless(ctx, "claude")
        self.assertTrue(e["exact"])
        self.assertAlmostEqual(e["points"], 0.04)
        self.assertIn("claude week 14%", self.ade("telemetry")[1])  # no role names claude here; it shows because it ran
        # With interactive spend in the window the jump is split by dollars, and the call is only an estimate.
        telemetry.note(ctx, "claude", "status line", {"seven_day": {"used_percentage": 18, "resets_at": later}},
                       start + timedelta(minutes=20), "s1", 1.3)
        records.append(ctx, "usage", {"card": "c2", "stage": "build", "role": "builder", "provider": "claude",
                                      "model": "sonnet", "wall_s": 0, "cost": 0.3, "at": iso(start + timedelta(minutes=15))})
        est = {x["card"]: x for x in telemetry.headless(ctx, "claude")}
        self.assertFalse(est["c2"]["exact"])
        self.assertAlmostEqual(est["c2"]["points"], 0.02)

    def test_an_unreported_claude_week_reads_as_api_key_only_when_that_secret_is_configured(self):
        # Guards the week line: API-key billing has no week to report, so it must not read like a fault; without the secret it still does.
        self.install()
        ctx = config.load(str(self.repo))
        ctx.cfg.setdefault("providers", {}).setdefault("claude", {}).pop("secrets", None)
        # Also guards the week line's meaning: most input arrives as cache, so cache reads must count toward "k tokens".
        telemetry.record(ctx, "c1", "build", "builder", "claude", "sonnet", 1.0, {"cache_read": 5000})
        with mock.patch.object(telemetry, "meters", return_value=[("claude", "not reported", 0.5, False)]):
            text = telemetry_verb.meter_text(ctx)
            self.assertIn("week not reported", text)
            self.assertIn("5k tokens", text)
            ctx.cfg["providers"]["claude"]["secrets"] = {"ANTHROPIC_API_KEY": "x"}
            text = telemetry_verb.meter_text(ctx)
        self.assertIn("claude on API key", text)
        self.assertNotIn("week not reported", text)

    def test_a_fake_loop_counts_its_calls_and_the_week_view_counts_a_message_once(self):
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        self.assertEqual(self.ade("loop", "c1")[0], 0)
        rows = records.read(config.load(str(self.repo)), "usage")
        self.assertEqual([(r["stage"], r["provider"], r["tokens_in"]) for r in rows], [("build", "fake", 0), ("review", "fake", 0)])
        code, out = self.ade("telemetry")
        self.assertEqual(code, 0, out)
        self.assertIn("fake", out)
        # Guards `telemetry --week`: per-day totals from two days of transcripts, a message written once per content
        # block counted once, sidechains split out, and --json carrying the same data as the text.
        cfg, now = Path(tempfile.mkdtemp()), datetime.now(timezone.utc)
        (cfg / "projects/p").mkdir(parents=True)

        def line(days, mid, out, side=False):
            return json.dumps({"type": "assistant", "timestamp": (now - timedelta(days=days)).isoformat(), "requestId": "r" + mid,
                               "entrypoint": "cli", "isSidechain": side, "message": {"id": mid, "model": "claude-opus-5-5",
                               "usage": {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5}}})
        (cfg / "projects/p/s.jsonl").write_text("\n".join([line(1, "a", 20), line(1, "a", 20), line(1, "a", 20), line(0, "b", 40, True), "{torn"]))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cfg), "CODEX_HOME": str(cfg)}):
            code, out = self.ade("telemetry", "--week", "--json")
            d = json.loads(out)
            self.assertEqual((code, d["total"]["tokens"], sorted(d["by_day"])), (0, 135 + 155, sorted({(now - timedelta(days=n)).date().isoformat() for n in (0, 1)})))
            self.assertEqual([v["tokens"] for v in d["by_day"].values()], [135, 155])
            self.assertEqual(d["by_thread"]["subagent"]["tokens"], 155)
            self.assertIn("by surface:", self.ade("telemetry", "--week")[1])
            self.assertEqual(self.ade("telemetry", "--json")[0], 2)


class PointsTest(FixtureCase):
    """Guards the kit meter model: the shipped copy equals the site's params, and points() prices a row by its formula."""

    def ctx(self, **usage):
        ctx = config.load_at(self.repo)
        ctx.cfg.setdefault("usage", {}).update(usage)
        return ctx

    def test_codex_rows_show_the_share_measured_from_meter_jumps(self):
        # Guards [usage] unit meter/plan_dollars: codex's share is headless() points labelled measured (per card and
        # over 7 days); with no jump the old wording stays; dollars output never changes.
        now = datetime.now(timezone.utc)
        later = (now + timedelta(days=3)).isoformat(timespec="seconds")
        ctx = self.ctx(unit="meter")
        self.assertEqual(telemetry_verb.measured(ctx), ({}, {}))  # no readings: no measured share
        for mins, used in ((0, 10), (10, 11)):
            telemetry.note(ctx, "codex", "t", {"primary": {"used_percent": used, "window_minutes": 10080,
                                                           "resets_at": later}}, now - timedelta(hours=1) + timedelta(minutes=mins))
        records.append(ctx, "usage", {"card": "c1", "stage": "build", "role": "builder", "provider": "codex",
                                      "model": "m", "wall_s": 0, "cost": 0.5,
                                      "at": (now - timedelta(hours=1) + timedelta(minutes=5)).isoformat(timespec="seconds")})
        by_card, by_week = telemetry_verb.measured(ctx, now - timedelta(days=7))
        self.assertAlmostEqual(by_card["c1"], 0.01)
        self.assertAlmostEqual(by_week["codex"], 0.01)
        t = {"calls": 1, "cost": 0.5, "wall_s": 0, "tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cache_write": 0}
        self.assertIn("1.0% of week (measured from meter jumps)", telemetry_verb._row("c1", t, None, ctx, 0.01))
        # Guards: codex's measured share stays a percent under plan_dollars (the plan price is Claude's), never a '$' figure.
        row = telemetry_verb._row("c1", t, None, self.ctx(unit="plan_dollars"), 0.01)
        self.assertIn("1.0% of week (measured from meter jumps)", row)
        self.assertNotIn("$", row)
        self.assertIn("$0.50", telemetry_verb._row("c2", t, None, ctx, None))  # no jump share: today's wording
        self.assertEqual(telemetry_verb.measured(self.ctx()), ({}, {}))  # dollars: nothing measured, output unchanged

    def test_the_kits_meter_model_is_the_sites_params(self):
        # Guards the kit's copy of the meter model: it is byte-for-byte the site's params, so both price a row alike.
        self.assertEqual(telemetry.METER.read_bytes(), (Path(config.KIT) / "site/data/burn/params.json").read_bytes())
