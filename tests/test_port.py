# id: tests.test_port
# what: what porting needed of the kit: first init reads trunk, mirrors, deploy triggers and deps; dependencies shared into lanes; quiet headers
# cmd: python3 -m unittest tests/test_port.py
# reads: tests/fixture_repo
# writes: temporary directories only
import contextlib
import os
import subprocess
from unittest import mock

from ade import config, host
from ade.stages import lane  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase


class PortTest(FixtureCase):
    def setUp(self):
        super().setUp()
        # A real project's trunk is master, and its dependencies are a gitignored folder no worktree checks out.
        self.git("branch", "-m", "master")
        (self.repo / ".gitignore").write_text("vendor/\n")
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "__init__.py").write_text("")
        (self.repo / "vendor" / "pad.py").write_text("WIDTH = 5\n")
        t = self.repo / "tests" / "test_calc.py"
        t.write_text(t.read_text().replace("from app.calc import add\n", "from app.calc import add\nfrom vendor.pad import WIDTH\n")
                     .replace("self.assertEqual(add(2, 3), 5)", "self.assertEqual(add(2, 3), WIDTH)"))
        self.git("add", "-A")
        self.git("commit", "-qm", "an existing project on master")

    def install_sharing(self):
        code, out = self.ade("init", "--no-commit")
        self.assertEqual(code, 0, out)
        toml = self.repo / "ade.toml"
        toml.write_text(toml.read_text().replace("share = []", 'share = ["vendor"]'))
        self.git("add", "-A")
        self.git("commit", "-qm", "install ade")

    def test_unshare_removes_the_link_and_keeps_its_target_and_on_windows_without_symlink_rights_a_junction(self):
        # CI runners are admins, so os.symlink works there; a Windows machine without developer mode takes the junction branch.
        self.install_sharing()
        junction = mock.patch("os.symlink", side_effect=OSError("symlinks need developer mode"))
        for i, links in enumerate([contextlib.nullcontext()] + ([junction] if os.name == "nt" else [])):
            lp = self.tmp / f"lane{i}"
            lp.mkdir()
            with links:
                host.link_dir(self.repo / "vendor", lp / "vendor")
            if links is junction:
                self.assertFalse(os.path.islink(lp / "vendor"))  # a junction, not a symlink
            self.assertTrue(host.is_link(lp / "vendor"))
            self.assertTrue((lp / "vendor" / "pad.py").is_file())
            lane.unshare(config.load(str(self.repo)), lp)
            self.assertFalse(os.path.lexists(lp / "vendor"))
            self.assertEqual((self.repo / "vendor" / "pad.py").read_text(), "WIDTH = 5\n")

    def test_a_lane_runs_with_shared_dependencies_never_commits_them_and_uninstall_keeps_the_users_excludes(self):
        self.install_sharing()
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text("mine.log\n")
        code, out = self.ade("card", "add", "note", "--path", "app/calc.py", "--done", "noted")
        self.assertEqual(code, 0, out)
        code, out = self.ade("loop", "c1")
        self.assertEqual(code, 0, out)  # the test imports vendor/, so it passed only through the lane's link
        self.assertIn("landed", out)
        self.assertEqual(self.git("ls-files", "vendor"), "")  # the link was never committed
        self.assertEqual((self.repo / "vendor" / "pad.py").read_text(), "WIDTH = 5\n")  # removing the lane left it whole
        self.assertIn("/vendor\n", exclude.read_text())
        exclude.write_text(exclude.read_text() + "later.log\n")  # a line the user adds after ade's block
        self.ade("card", "add", "again", "--path", "app/calc.py", "--done", "noted")
        self.assertEqual(self.ade("loop", "c2")[0], 0)  # a lane pass rewrites the block and keeps both lines
        self.assertEqual(exclude.read_text().count("/vendor"), 1)
        self.assertIn("later.log", exclude.read_text())
        code, out = self.ade("uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(exclude.read_text(), "mine.log\nlater.log\n")

    def test_headers_go_unmentioned_where_land_does_not_ask_for_them(self):
        code, out = self.ade("init")
        self.assertEqual(code, 0, out)
        (self.repo / "app" / "bare.py").write_text("X = 1\n")
        code, out = self.ade("graph")
        self.assertNotIn("five-line header", out)
        toml = self.repo / "ade.toml"
        toml.write_text(toml.read_text() + '\n[graph]\nrequire = ["header"]\n')
        code, out = self.ade("graph")
        self.assertIn("without a five-line header: app/bare.py", out)


class SurveyTest(FixtureCase):
    """An existing repo's quirks are read at first init and fitted with safe values, not left to a port doc."""

    def quirks(self, url):
        self.git("branch", "-m", "master")
        self.git("remote", "add", "origin", url)
        self.git("config", "--add", "remote.origin.push", "refs/heads/master:refs/heads/master")
        self.git("config", "--add", "remote.origin.push", "refs/heads/master:refs/heads/main")
        wf = self.repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ship.yml").write_text("on:\n  push:\n    branches: [main]\njobs:\n  go:\n    steps:\n"
                                     "      - run: npx wrangler pages deploy dist\n")
        (self.repo / "package.json").write_text('{"scripts": {"typecheck": "tsc --noEmit"}}\n')
        (self.repo / ".gitignore").write_text("node_modules/\n")
        (self.repo / "node_modules").mkdir()
        self.git("add", "-A")
        self.git("commit", "-qm", "an existing project with a mirror and a deploy on push")

    def test_first_init_fits_trunk_mirror_deploy_and_deps_and_says_why(self):
        self.quirks("git@github.com:someone/app.git")
        code, out = self.ade("init")
        self.assertEqual(code, 0, out)
        cfg = config.load(str(self.repo)).cfg
        self.assertEqual(cfg["git"]["main"], "master")
        self.assertEqual(cfg["git"]["mirror"], ["master:main"])
        self.assertEqual(cfg["git"]["push"], "manual")  # pushing main deploys, so landings never push on their own
        self.assertEqual(cfg["lanes"]["share"], ["node_modules"])
        self.assertEqual(cfg["tests"]["always"], ["npm run typecheck"])
        for word in ("trunk:", "mirror:", "deploy:", "deps:"):
            self.assertIn(word, out)
        flow = (self.repo / ".github/workflows/ade-mirror.yml").read_text()
        self.assertIn('"$GITHUB_SHA:refs/heads/main"', flow)
        self.assertNotIn("+", flow)  # fast-forward only

    def test_without_github_ade_push_sends_the_mirror_fast_forward_only(self):
        bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.quirks(str(bare))
        self.assertEqual(self.ade("init")[0], 0)
        self.assertFalse((self.repo / ".github/workflows/ade-mirror.yml").exists())
        code, out = self.ade("push")
        self.assertEqual(code, 0, out)
        main = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=bare, capture_output=True, text=True)
        self.assertEqual(main.stdout.strip(), self.git("rev-parse", "master").strip())
