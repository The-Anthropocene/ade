# id: tests.test_providers
# what: the seatbelt profile denies reads of each secrets glob, with every regex character escaped
# cmd: python3 -m unittest tests/test_providers.py
# reads: ade/providers.py
# writes: nothing
import os
import re
import tempfile
import unittest
from unittest import mock

from ade import providers


def _rx(profile):
    # the SBPL #"..." literal is raw, so the text is already the Python regex
    return [re.compile(m) for m in re.findall(r'\(regex #"(.*)"\)\)', profile)]


class SeatbeltProfile(unittest.TestCase):
    def test_profile_shape_and_matching(self):
        # Guards the profile format (header, one deny per entry) and the glob rules: * stays in a name, a bare glob
        # matches in any directory, ~ is expanded, and regex characters in a name are literal.
        prof = providers._seatbelt_profile(["id_rsa*", ".env.*", "~/.aws/*", "/etc/s?cret"])
        self.assertEqual(prof.count("(deny file-read*"), 4)
        rsa, env, aws, sec = _rx(prof)
        self.assertTrue(rsa.search("/a/b/id_rsa.pub"))
        self.assertFalse(rsa.search("/a/id_rsa/x"))
        self.assertTrue(env.search("/w/.env.local"))
        self.assertFalse(env.search("/w/xenv.local"))
        self.assertTrue(aws.search(os.path.expanduser("~/.aws/creds")))
        self.assertTrue(sec.search("/etc/secret"))
        self.assertFalse(sec.search("/etc/s/cret"))
        # Guards the SBPL raw #"..." literal: backslashes are written once (.env -> \.env), and a quote is refused.
        prof = providers._seatbelt_profile([".env"])
        self.assertIn(r"\.env", prof)
        self.assertNotIn("\\\\.env", prof)
        with self.assertRaises(ValueError):
            providers._seatbelt_profile(['a"b'])

    def test_secret_ok_allowed_after_denies_by_resolved_and_given_path(self):
        # Guards the seatbelt rule order (last match wins): a literal allow for each existing secret_ok file under cwd comes
        # after every deny, and only for files that exist and match.
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)  # a Windows 8.3 temp dir (RUNNER~1) would otherwise add a second allow line
            for n in (".env", ".env.example"):
                open(os.path.join(d, n), "w").close()
            prof = providers._seatbelt_profile([".env*"], [".env.example"], d)
        lines = prof.splitlines()
        allow = [i for i, l in enumerate(lines) if l.startswith("(allow file-read*")]
        self.assertEqual(len(allow), 1)
        # The profile writes the path SBPL-escaped (backslashes, quotes), so expect it escaped (C:\ paths on Windows).
        esc = os.path.join(d, ".env.example").replace("\\", "\\\\").replace('"', '\\"')
        self.assertIn(esc, lines[allow[0]])
        self.assertNotIn(os.path.join(d, ".env") + '"', prof)
        self.assertGreater(allow[0], max(i for i, l in enumerate(lines) if l.startswith("(deny")))

        # Guards that seatbelt, which matches resolved paths, gets the realpath literal as well as the path as given.
        def esc(p):
            return p.replace("\\", "\\\\").replace('"', '\\"')

        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            real = os.path.join(d, "real")
            os.mkdir(real)
            open(os.path.join(real, ".env.example"), "w").close()
            link = os.path.join(d, "link")
            try:
                os.symlink(real, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            prof = providers._seatbelt_profile([".env*"], [".env.example"], link)
            self.assertIn('(literal "' + esc(os.path.realpath(os.path.join(link, ".env.example"))) + '")', prof)
            self.assertIn('(literal "' + esc(os.path.join(link, ".env.example")) + '")', prof)


class BwrapArgs(unittest.TestCase):
    def test_hides_secrets_but_not_allowed_and_skips_dependency_and_share_trees(self):
        # Guards the argv contract: --dev-bind / / first, one --ro-bind /dev/null per secret file, -- last, secret_ok kept visible.
        with tempfile.TemporaryDirectory() as d:
            for n in (".env", ".env.example", "a.txt"):
                open(os.path.join(d, n), "w").close()
            args = providers._bwrap_args([".env*"], [".env.example"], d, d)
            self.assertEqual(args[:3], ["--dev-bind", "/", "/"])
            self.assertEqual(args[-1], "--")
            self.assertEqual(args[3:-1], ["--ro-bind", "/dev/null", os.path.join(d, ".env")])
        # Guards the walk's pruning: a secret-named file inside node_modules or a [lanes] share folder is not covered; the root one is.
        with tempfile.TemporaryDirectory() as d:
            for sub in ("node_modules/x", "shared"):
                os.makedirs(os.path.join(d, sub))
                open(os.path.join(d, sub, ".env"), "w").close()
            open(os.path.join(d, ".env"), "w").close()
            args = providers._bwrap_args([".env*"], [], d, d, ["shared"])
            self.assertEqual(args[3:-1], ["--ro-bind", "/dev/null", os.path.join(d, ".env")])


class OsGuard(unittest.TestCase):
    # Guards the contract: the prefix is sandbox-exec -p <profile> on macOS, bwrap args on Linux with bwrap, None elsewhere.
    def test_three_cases(self):
        from pathlib import Path
        from types import SimpleNamespace
        ctx = SimpleNamespace(cfg={"guards": {"secrets": [".env*"]}}, state=Path("/x/.ade"))
        have = lambda n: "/bin/" + n
        for plat, which, head in (("darwin", have, "sandbox-exec"), ("linux", have, "bwrap"),
                                  ("linux", lambda n: None, None), ("win32", have, None)):
            with mock.patch("sys.platform", plat), mock.patch("shutil.which", which):
                got = providers.os_guard(ctx, "/nonexistent")
            self.assertEqual(got and got[0], head)
            if head == "sandbox-exec":
                self.assertEqual(got[1], "-p")
        # guards the contract: a lane log that vanishes mid-call is a provider.failed stop naming the cure, with the call recorded as failed
        import sys
        from ade.refuse import Refusal
        if os.name == "nt":  # Windows cannot delete the log while the call holds it open
            return
        with tempfile.TemporaryDirectory() as d:
            kill = "import os,sys;os.remove(sys.argv[1])"
            ctx = SimpleNamespace(state=Path(d), root=Path(d), cfg={"roles": {"b": {"provider": "e", "models": {"e": "m"}}},
                                                      "permissions": {"lane_mode": "x"},
                                                      "providers": {"e": {"cmd": [sys.executable, "-c", kill, str(Path(d) / "lanes" / "c1.log")]}}})
            with mock.patch.object(providers.telemetry, "record") as rec, mock.patch.object(providers, "effort_args", return_value=[]), \
                    mock.patch.object(providers, "compaction_args", return_value=[]), mock.patch.dict(os.environ, {"ADE_PROVIDER": ""}):
                with self.assertRaises(Refusal) as cm:
                    providers.call(ctx, "b", "p", d, {"id": "c1"})
            self.assertEqual(cm.exception.code, "provider.failed")
            self.assertIn("ade loop c1", cm.exception.cure)
            self.assertIn("c1.log", cm.exception.cause)
            self.assertIn("failed", rec.call_args[0][-1])
            # guards the contract: a prompt over BIG_PROMPT goes on stdin, not argv, and an OSError from Popen is a clean provider.failed stop
            ctx.cfg["roles"]["b"] = {"provider": "codex", "models": {"codex": "m"}}
            ctx.cfg["providers"] = {"codex": {"cmd": [sys.executable, "-c", "pass"]}}
            big = "x" * (providers.BIG_PROMPT + 1)
            with mock.patch.object(providers.telemetry, "record"), mock.patch.object(providers, "effort_args", return_value=[]), \
                    mock.patch.object(providers, "compaction_args", return_value=[]), mock.patch.dict(os.environ, {"ADE_PROVIDER": ""}), \
                    mock.patch.object(providers, "argv_for", return_value=[sys.executable, "-c", "pass", big]), \
                    mock.patch.object(providers.subprocess, "Popen", side_effect=OSError(7, "Argument list too long")) as popen:
                with self.assertRaises(Refusal) as cm:
                    providers.call(ctx, "b", big, d, {"id": "c1"})
            self.assertNotIn(big, popen.call_args[0][0])
            self.assertEqual(cm.exception.code, "provider.failed")
            self.assertIn("Argument list too long", cm.exception.cause)

    def test_template_opencode_is_guarded_and_stall_bounded(self):
        # guards the contract that opencode lanes run under the OS read guard, with a positive stall bound below the timeout
        import tomllib
        from ade.config import TEMPLATE
        spec = tomllib.loads(TEMPLATE.read_text())["providers"]["opencode"]
        self.assertEqual(spec.get("sandbox"), "os")
        self.assertEqual(tomllib.loads(TEMPLATE.read_text())["providers"]["pi"].get("sandbox"), "os")
        self.assertTrue(0 < spec["stall"] < spec["timeout"])
        # guards the read-only reviewer: a role's args reach that engine's argv, and only the reviewer's
        from types import SimpleNamespace
        ctx = SimpleNamespace(cfg=tomllib.loads(TEMPLATE.read_text()))
        argv = lambda role: providers.argv_for(ctx, role, "opencode", "go", ".", None)
        self.assertEqual(argv("reviewer")[-2:], ["--agent", "plan"])
        self.assertNotIn("--agent", argv("builder"))
        # guards the Pi contract: {prompt_file} (inside an argument) is the prompt written to .ade/lanes/<card>.<role>.prompt.md
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            ctx.state = Path(d)
            pi = providers.argv_for(ctx, "builder", "pi", "multi\nline", ".", {"id": "c9"})
            pf = Path(d) / "lanes" / "c9.builder.prompt.md"
            self.assertIn(f"@{pf}", pi)
            self.assertEqual(pf.read_text(), "multi\nline")
            self.assertNotIn("multi\nline", pi)

    def test_effort_flags_per_engine_and_unset(self):
        # guards the contract: a role's effort becomes each engine's own flag, and no effort passes no flag
        from types import SimpleNamespace
        ctx = SimpleNamespace(cfg={"roles": {"r": {"effort": {"claude": "high", "codex": "low", "opencode": "max"}}, "u": {}}})
        self.assertEqual(providers.effort_args(ctx, "u", "codex"), [])
        ctx.cfg["providers"] = {"codex": {"default_effort": "medium"}}  # unset codex effort takes the default; a role's wins
        self.assertEqual(providers.effort_args(ctx, "u", "codex"), ["-c", "model_reasoning_effort=medium"])
        self.assertEqual(providers.effort_args(ctx, "r", "codex"), ["-c", "model_reasoning_effort=low"])
        # guards the profile contract: [profiles] select picks by the first glob that matches a card path, a card's own
        # profile overrides the globs, the profile merges over the role per engine, and a role without it is itself
        ctx.cfg["roles"]["r"].update(models={"claude": "opus", "codex": "m"},
                                     profiles={"docs": {"effort": {"claude": "low"}, "models": {"claude": "haiku"}}})
        ctx.cfg["profiles"] = {"select": {"docs/*": "docs", "*": "other"}}
        docs, own = {"paths": ["src/a.py", "docs/x.md"]}, {"paths": ["docs/x.md"], "profile": "mine"}
        self.assertEqual([providers.profile(ctx, c) for c in (docs, own, {"paths": ["a"]})], ["docs", "mine", "other"])
        self.assertEqual(providers.effort_args(ctx, "r", "claude", docs), ["--effort", "low"])
        self.assertEqual(providers.role_cfg(ctx, "r", docs)["models"], {"claude": "haiku", "codex": "m"})
        # a profile's provider sets the role's engine for its card; an explicit name wins over it
        ctx.cfg["roles"]["r"].update(provider="claude")
        ctx.cfg["roles"]["r"]["profiles"]["docs"]["provider"] = "codex"
        with mock.patch.dict(os.environ, {"ADE_PROVIDER": ""}):
            self.assertEqual([providers.engine(ctx, "r", card=docs), providers.engine(ctx, "r", "x", docs),
                              providers.engine(ctx, "r")], ["codex", "x", "claude"])
        # guards the lane contract: claude lanes disallow the Agent tool unless the seat opts in, and a system_prompt file
        # is appended (never replacing), the role's own key over its profile's, a missing file refusing
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            ctx.root, ctx.cfg["permissions"] = Path(d), {"lane_mode": "x"}
            ctx.cfg["providers"] = {"claude": {"cmd": ["claude", "{prompt}", "--model", "{model}"]}}
            ctx.cfg["roles"]["r"]["profiles"]["docs"]["system_prompt"] = "p.md"
            argv = lambda: providers.argv_for(ctx, "r", "claude", "go", d, docs)
            (Path(d) / "p.md").write_text("hi")
            self.assertEqual(argv()[-4:], ["--append-system-prompt-file", str(Path(d) / "p.md"), "--disallowedTools", "Agent"])
            ctx.cfg["roles"]["r"].update(system_prompt="q.md", subagents=True)
            (Path(d) / "q.md").write_text("hi")
            self.assertEqual(argv()[-2:], ["--append-system-prompt-file", str(Path(d) / "q.md")])
            (Path(d) / "q.md").unlink()
            with self.assertRaises(Exception):
                argv()
            del ctx.cfg["roles"]["r"]["system_prompt"], ctx.cfg["roles"]["r"]["subagents"]
            del ctx.cfg["roles"]["r"]["profiles"]["docs"]["system_prompt"]
        # guards the per-flight override: --model/--effort reach the role's config for this run only, then vanish
        import json
        from argparse import Namespace
        from ade import config
        from ade.verbs import loop
        cfg = {"roles": {"r": {"provider": "codex", "models": {"codex": "m"}}}}
        with mock.patch.dict(os.environ):  # a conduct run's own ADE_FLIGHT must not leak in
            os.environ.pop("ADE_FLIGHT", None)
            with loop.flight(Namespace(model=["r=claude:haiku"], effort=["r=max"])):
                config._flight(cfg, json.loads(os.environ["ADE_FLIGHT"]))
            self.assertEqual(cfg["roles"]["r"]["provider"], "claude")
            self.assertEqual(cfg["roles"]["r"]["models"]["claude"], "haiku")
            self.assertEqual(providers.effort_args(SimpleNamespace(cfg=cfg), "r", "claude"), ["--effort", "max"])
            self.assertNotIn("ADE_FLIGHT", os.environ)


if __name__ == "__main__":
    unittest.main()
