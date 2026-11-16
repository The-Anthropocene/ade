# id: tests.helpers
# what: build a fresh, isolated fixture repo and run ade verbs in-process against it
# cmd: -
# reads: tests/fixture_repo
# writes: temporary directories only
import atexit
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ade import cli, firstrun
from ade.lib import agent

FIXTURE = Path(__file__).with_name("fixture_repo")

_PREBUILT = {}


def _prebuilt(fixture):
    """The committed fixture repo, built once per fixture directory in this process under its own isolated git config."""
    fixture = Path(fixture)
    if fixture not in _PREBUILT:
        root = Path(tempfile.mkdtemp(prefix="ade-fixture-")).resolve()
        atexit.register(shutil.rmtree, root, True)
        cfg = root / "gitconfig"
        cfg.write_text("[user]\n\tname = ADE Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n")
        env = {**os.environ, "GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1"}
        repo = root / "repo"
        shutil.copytree(fixture, repo, ignore=shutil.ignore_patterns("__pycache__"))
        for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-qm", "fixture")):
            subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
        _PREBUILT[fixture] = repo
    return _PREBUILT[fixture]


class FixtureCase(unittest.TestCase):
    """Each test gets its own repo, lane root and git config, so nothing on the machine is read or touched."""

    fixture = FIXTURE

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ade-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        gitcfg = self.tmp / "gitconfig"
        gitcfg.write_text("[user]\n\tname = ADE Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n")
        # Orca too: a missing binary unless a test stands one in, so a machine running the real Orca is never mirrored into.
        env = {"ADE_WORK": str(self.tmp / "work"), "ADE_PROVIDER": "fake", "CODEX_HOME": str(self.tmp / "codex"), "GIT_CONFIG_GLOBAL": str(gitcfg),
               "GIT_CONFIG_NOSYSTEM": "1", "XDG_CONFIG_HOME": str(self.tmp / "config"), "APPDATA": str(self.tmp / "config"),
               "ORCA_CLI_COMMAND": str(self.tmp / "no-orca")}
        environ = mock.patch.dict(os.environ, env)
        environ.start()
        self.addCleanup(environ.stop)
        # A conduct run's own flight override (ADE_FLIGHT) must not reach the repo under test.
        # Codex's sandbox markers too: a Codex builder runs the tests inside its sandbox, and lane.sandboxed would otherwise refuse every fixture lane.
        # The engine markers too: a lane runs the suite inside an agent, and the fixture's answers must read as a person's.
        for k in ("ORCA_DEV_REPO_ROOT", "ORCA_RUNTIME_CLIENT", "ADE_FLIGHT", "ADE_FLIGHT_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
                  *(n for _, names in agent.MARKERS for n in names)):
            os.environ.pop(k, None)
        # The machine defaults folder follows XDG_CONFIG_HOME on every platform here, so a real defaults file never leaks in.
        patcher = mock.patch.object(firstrun, "config_dir", lambda: Path(os.environ["XDG_CONFIG_HOME"]) / "ade")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.tmp / "repo"
        shutil.copytree(_prebuilt(self.fixture), self.repo)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def ade(self, *argv, stdin="", cwd=None):
        out, err = io.StringIO(), io.StringIO()
        old_stdin, sys.stdin = sys.stdin, io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv), cwd=str(cwd or self.repo))
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue() + err.getvalue()

    def install(self):
        code, out = self.ade("init")
        self.assertEqual(code, 0, out)
        # A first ade.toml holds only what init chose; many tests edit a default key in place, so they get the full template
        # text, with the chosen keys (survey, root lane_mode) put back, committed as one step.
        toml = self.repo / "ade.toml"
        if toml.is_file() and toml.read_text().startswith(firstrun.HEADER):
            import tomllib
            from ade import survey
            mine, full = tomllib.loads(toml.read_text()), firstrun.TEMPLATE.read_text()
            pick = {k: v for k, v in mine.get("git", {}).items() if k in ("main", "push", "mirror")}
            pick |= {k: v for k, v in {**mine.get("tests", {}), **mine.get("lanes", {})}.items() if k in ("command", "always", "share")}
            full = survey.apply(full, pick)
            if "lane_mode" in mine.get("permissions", {}):
                full = full.replace('lane_mode = "bypassPermissions"', f'lane_mode = "{mine["permissions"]["lane_mode"]}"', 1)
            toml.write_text(full)
            self.git("commit", "-qam", "full ade.toml for the test")
