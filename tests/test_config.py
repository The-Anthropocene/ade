# id: tests.test_config
# what: config.load finds the main checkout from a main checkout, a linked worktree and a non-repo, spawning git once
# cmd: python3 -m unittest tests/test_config.py
# reads: -
# writes: temporary directories only
import argparse
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ade import config, providers
from ade.lib import probe
from ade.verbs import config as config_verb
from ade.verbs import graph as graph_verb


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class MainWorktree(unittest.TestCase):
    def test_root_is_the_main_checkout_with_one_git_spawn(self):
        # Guards the contract: the same root from any worktree, None outside a repo, and one git spawn per lookup.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            main = base / "main"
            main.mkdir()
            git(main, "init", "-q")
            git(main, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
            git(main, "worktree", "add", "-q", str(base / "lane"))
            (base / "plain").mkdir()
            # Also guards `ade graph` in a lane: [graph] comes from the lane's ade.toml, the main checkout's stays as it was.
            (main / "ade.toml").write_text("")
            (base / "lane" / "ade.toml").write_text('[graph]\nexclude = ["skip_me.py"]\n')
            for w in (main, base / "lane"):
                (w / "skip_me.py").write_text("x = 1\n")
                (w / "keep_me.py").write_text("y = 1\n")
                graph_verb.run(argparse.Namespace(file=None, check=False, route=False), w)
            self.assertNotIn("skip_me.py", (base / "lane" / "graph.json").read_text())
            self.assertIn("keep_me.py", (base / "lane" / "graph.json").read_text())
            self.assertIn("skip_me.py", (main / "graph.json").read_text())
            self.assertEqual(config.main_worktree(main), main)
            self.assertEqual(config.main_worktree(base / "lane"), main)
            self.assertIsNone(config.main_worktree(base / "plain"))
            with mock.patch("ade.config.subprocess.run", wraps=subprocess.run) as run:
                config.main_worktree(base / "lane")
            self.assertEqual(run.call_count, 1)
        # Also guards `ade config`: every [section] in the template appears in its no-word listing.
        secs, keys = config_verb.parse()
        listed = "\n".join(config_verb.listing(secs, keys))
        for name, _ in secs:
            self.assertIn(f"[{name}]", listed)
        # Also guards the example rule: a commented `key = value` example is its own row, marked as unset.
        self.assertTrue(any(s.startswith("roles.") and k == "effort" and d[0] == "unset; template example"
                            for s, k, d in keys))
        # Also guards `ade config --roles`: every engine in a role's models table gets a model line, the active one marked.
        ctx0 = config.load(Path.cwd())
        shown = "\n".join(config_verb.roles(ctx0, {}, ""))
        for role in ctx0.cfg["roles"].values():
            for eng in role["models"]:
                self.assertIn(f"model {eng}", shown)
        self.assertIn("(active)", shown)
        # Also guards the effort source: each effort row says 'role', 'default_effort' or the engine's own setting; codex is never bare.
        for line in (l for l in shown.splitlines() if l.strip().startswith("effort ")):
            self.assertRegex(line, r"\((role|default_effort)\)|engine's own setting")
            if "effort codex" in line:
                self.assertNotIn("engine's own setting", line)
        # Also guards `ade config --seats` and the [report] seats line: the 'config:' format (role engine model effort profile
        # system_prompt ttl settings argv) for every role and engine, the prompt never inlined, a secret's name only set/unset,
        # a host behaviour variable scrub_env drops and a settings file's model each a 'conflict:' line.
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"MAX_THINKING_TOKENS": "9", "X_API_KEY": "sk-1"}), \
                mock.patch("pathlib.Path.home", return_value=Path(d)):
            (Path(d) / ".claude").mkdir()
            (Path(d) / ".claude" / "settings.json").write_text('{"model": "haiku", "env": {"MAX_THINKING_TOKENS": "1"}}')
            seats = "\n".join(providers.seats(ctx0))
            card = {"id": "c1", "paths": []}
            line = providers.seat_line(ctx0, "builder", "claude", card,
                                       providers.argv_for(ctx0, "builder", "claude", "do\nit", d, card), {}, "do\nit")
        fmt = r"config: \S+ \S+ \S+ \S+ \S+ system_prompt=([0-9a-f]{8}|none) ttl=\S+ settings=\S+ argv=.+"
        self.assertRegex(line, "^" + fmt + "\n$")
        self.assertNotIn("do\nit", line)
        for role in ctx0.cfg["roles"].values():
            for eng in role["models"]:
                self.assertRegex(seats, rf"  config: \S+ {eng} \S+ \S+ \S+ system_prompt=")
        self.assertEqual(providers.shown(ctx0, {"X_API_KEY": "sk-1"}, "X_API_KEY"), "set")
        self.assertNotIn("sk-1", seats)
        self.assertIn("MAX_THINKING_TOKENS (changes behaviour)", seats)
        self.assertRegex(seats, r"conflict: host env sets MAX_THINKING_TOKENS=9; scrub_env drops it")
        self.assertRegex(seats, r"conflict: \S+settings.json sets model haiku; ADE's --model \S+ overrides it")
        self.assertRegex(seats, r"conflict: \S+settings.json env sets MAX_THINKING_TOKENS")
        # Also guards `ade config --probe`: under the fake provider every role answers, a line each, exit 0, no spend.
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", dict(ADE_PROVIDER="fake")), \
                mock.patch("builtins.print") as out:
            ctx = config.load(Path.cwd())
            ctx.root = Path(d)
            self.assertEqual(probe.roles(ctx, d), 0)
        self.assertEqual(out.call_count, 1 + len(ctx.cfg["roles"]))
        # Also guards `ade config --probe --guards`: the fake spends nothing, reports blocked, records one guard_probe row.
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", dict(ADE_PROVIDER="fake")), \
                mock.patch("builtins.print") as out, mock.patch("ade.records.append") as rec:
            ctx.root = Path(d)
            self.assertEqual(probe.guards(ctx), 0)
        self.assertEqual(rec.call_args.args[1:], ("guard_probe", dict(engine="fake", os=sys.platform, blocked=True)))
        self.assertIn("blocked", out.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
