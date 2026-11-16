# id: tests.test_install
# what: installing into a fresh repo: dry-run writes nothing, init commits only its own files, hooks stay machine-local, uninstall keeps the user's files
# cmd: python3 -m unittest tests/test_install.py
# reads: tests/fixture_repo
# writes: temporary directories only
import json
import os
import re
import sys
from pathlib import Path
from unittest import mock

from ade import cli, config, install, providers, stages
from ade.verbs import graph, hook, init  # noqa: F401  (imported so the graph records what this test covers)
from tests.helpers import FixtureCase


class InstallTest(FixtureCase):
    def test_init_commits_only_its_own_files_and_no_commit_commits_nothing(self):
        # Guards the contract: init's commit holds exactly the files it wrote, so the person's other changes stay theirs;
        # with --no-commit it writes the same files and commits none of them.
        (self.repo / "mine.txt").write_text("unrelated\n")
        head = self.git("rev-parse", "HEAD")
        code, out = self.ade("init", "--no-commit")
        self.assertEqual(code, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertTrue(self.git("status", "--porcelain").replace("?? mine.txt\n", "").strip())
        self.git("checkout", "-q", "--", ".")
        self.git("clean", "-fdxq", "-e", "mine.txt")
        self.install()
        self.assertNotEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain").strip(), "?? mine.txt")

    def test_dry_run_writes_nothing_and_the_committed_files_hold_no_machine_fact(self):
        # Guards: --dry-run touches nothing; hooks and the status line (machine paths) live only in the gitignored local
        # file, so a teammate's checkout is never dirtied; init twice changes nothing; the skill and docs name only real verbs and flags.
        code, out = self.ade("init", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.repo / ".claude").exists())

        self.install()
        settings = json.loads((self.repo / ".claude/settings.json").read_text())
        local = json.loads((self.repo / ".claude/settings.local.json").read_text())
        self.assertNotIn("hooks", settings)
        self.assertNotIn("statusLine", settings)
        self.assertIn(".claude/settings.local.json", (self.repo / ".gitignore").read_text().splitlines())
        self.assertNotIn("settings.local.json", self.git("ls-files"))
        commands = [h["command"] for v in local["hooks"].values() for e in v for h in e["hooks"]]
        self.assertTrue(commands and all(c.startswith(install.ade_cmd() + " ") for c in commands), commands)
        before = self.git("status", "--porcelain")
        self.assertEqual(self.ade("init")[0], 0)
        self.assertEqual(self.git("status", "--porcelain"), before)

        skill = (self.repo / ".claude/skills/ade/SKILL.md").read_text()
        # Guards both committed copies (Claude and Codex) against drifting from the generator.
        for rel in (".claude/skills/ade/SKILL.md", ".agents/skills/ade/SKILL.md"):
            self.assertEqual((Path(__file__).resolve().parent.parent / rel).read_text(), install.skill(), rel)

        # Guards the skill's references: every `ade <verb>` it names is a real verb, every `ade config <key>` a template word.
        template = (Path(install.__file__).parent / "template.toml").read_text()
        for verb, arg in re.findall(r"`ade ([a-z][a-z-]*)(?: ([a-z][\w.]*))?", skill):
            self.assertIn(verb, cli.verbs(), verb)
            if verb == "config" and arg:
                self.assertIn(arg, template, arg)
        # Guards the docs: every `ade VERB` span names a real verb and every --flag before ' -- ' is one that verb's parser takes
        # (release notes are skipped: history names retired verbs on purpose).
        root = Path(__file__).resolve().parent.parent
        def opts(p):  # every option string a parser takes, its subcommands' included
            return {o for a in p._actions for o in a.option_strings} | {o for a in p._actions if a.choices and hasattr(a.choices, "values") for sp in a.choices.values() for o in opts(sp)}
        flags = {v: opts(sp) for v, sp in next(a for a in cli.parser()[0]._actions if a.dest == "verb").choices.items()}
        for name, text in [("SKILL.md", skill)] + [(d.name, d.read_text()) for d in [root / "README.md", *sorted((root / "site/docs").glob("*.md"))] if d.name != "08-release-notes.md"]:
            for span in re.findall(r"`(ade [a-z][^`\n]*)`", text):
                words = span.split()
                self.assertIn(words[1], cli.verbs(), f"{name}: {span}")
                for flag in re.findall(r"(?<![\w-])--[a-z][\w-]*", span.split(" -- ")[0]):
                    self.assertIn(flag, flags[words[1]], f"{name}: {span}")

    def test_bash_deny_is_printed_even_when_the_refusal_row_cannot_be_backed_up(self):
        # Guards the contract: the guard's deny comes first and a failing records backup cannot undo it.
        self.install()
        event = {"session_id": "s1", "cwd": str(self.repo), "tool_input": {"command": "git push --force origin main"}}
        with mock.patch.dict("os.environ"), mock.patch("ade.records._backup", side_effect=RuntimeError("refused")):
            os.environ.pop("ADE_ROLE", None)
            code, out = self.ade("hook", "bash", stdin=json.dumps(event))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_grep_note_reads_any_result_shape_never_blocks_and_never_repeats(self):
        # Invariant: the note is context after the search; it never denies, and a file is noted once per session.
        self.install()
        out = "app/calc.py:3:def add\n./app/calc.py-4-  return\napp/calc.py\napp/calc.py:2\nbinary file matches\napp/cal"
        event = {"session_id": "s1", "cwd": str(self.repo), "tool_name": "Bash",
                 "tool_input": {"command": "rg -n add | head"}, "tool_response": {"stdout": out}}
        note = json.loads(self.ade("hook", "grep", stdin=json.dumps(event))[1])["hookSpecificOutput"]
        self.assertEqual(set(note), {"hookEventName", "additionalContext"})
        self.assertIn("app/calc.py: ", note["additionalContext"])
        self.assertIn("tests/test_calc.py", note["additionalContext"])
        self.assertEqual(self.ade("hook", "grep", stdin=json.dumps(event))[1], "")
        event.update(session_id="s2", tool_input={"command": "python -m app.calc"})
        self.assertEqual(self.ade("hook", "grep", stdin=json.dumps(event))[1], "")
        # Also guards: output from tool_output alone, an argv-list command, and an unknown tool name with a command is
        # read like Bash. The Codex payload is inferred, not captured (ARCHITECTURE.md 5).
        event = {"session_id": "s3", "cwd": str(self.repo), "tool_name": "shell",
                 "tool_input": {"command": ["rg", "-n", "add"]}, "tool_output": "app/calc.py:3:def add\n"}
        note = json.loads(self.ade("hook", "grep", stdin=json.dumps(event))[1])["hookSpecificOutput"]
        self.assertIn("app/calc.py: ", note["additionalContext"])
        event.update(session_id="s4", tool_input={"command": ["python", "-m", "app.calc"]})
        self.assertEqual(self.ade("hook", "grep", stdin=json.dumps(event))[1], "")

    def test_codex_hooks_file_is_one_merged_gitignored_entry_with_no_bypass_flag(self):
        # Guards the contract: .codex/hooks.json holds ADE's full HOOKS set beside the user's entry, init never
        # doubles it, uninstall strips only ADE's, and no ade code passes the hook-trust bypass flag.
        cp = self.repo / ".codex/hooks.json"
        cp.parent.mkdir()
        mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "npx mine"}]}
        cp.write_text(json.dumps({"hooks": {"PostToolUse": [mine]}}))
        self.install()
        self.assertEqual(self.ade("init")[0], 0)
        got = json.loads(cp.read_text())["hooks"]
        self.assertEqual(got["PostToolUse"][0], mine)
        flat = sorted((ev, e.get("matcher"), h["command"]) for ev, v in got.items() for e in v for h in e["hooks"] if h["command"] != "npx mine")
        self.assertEqual(flat, sorted((ev, m, f"{install.ade_cmd()} {verb}") for ev, m, verb in install.HOOKS))
        self.assertIn(".codex/hooks.json", (self.repo / ".gitignore").read_text().splitlines())
        self.assertEqual(self.ade("uninstall")[0], 0)
        self.assertEqual(json.loads(cp.read_text()), {"hooks": {"PostToolUse": [mine]}})
        for f in Path(install.__file__).parent.rglob("*"):
            if f.suffix in (".py", ".toml"):
                self.assertNotIn("--dangerously-bypass-hook-trust", f.read_text(encoding="utf-8"), f.name)

    def test_init_moves_hooks_out_of_the_committed_settings(self):
        # An older init put hooks (some by kit path) in settings.json; the user's own hook there stays.
        # Guards install._ours, which tells them apart: it knows every form init writes and claims no other command.
        for cmd in ("ade hook brief", '"C:/Users/x/.local/bin/ade.exe" hook bash', "'/Volumes/ADE/ade/bin/ade' hook brief",
                    '"/opt/py/bin/python3" -m ade status --line', '"/Volumes/ADE/.tools/bin/ade" hook start'):
            self.assertTrue(install._ours(cmd), cmd)
        for cmd in ("npx expo-hook", '"/usr/bin/adep" run', "python3 scripts/ade_check.py"):
            self.assertFalse(install._ours(cmd), cmd)
        self.install()
        sp, lp = self.repo / ".claude/settings.json", self.repo / ".claude/settings.local.json"
        settings = json.loads(sp.read_text())
        settings["statusLine"] = {"type": "command", "command": "ade status --line"}
        settings["hooks"] = {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "'/Volumes/ADE/ade/bin/ade' hook brief"}]},
                                            {"matcher": "Bash", "hooks": [{"type": "command", "command": "npx expo-hook"}]}]}
        sp.write_text(json.dumps(settings))
        lp.write_text(json.dumps({"permissions": {"allow": ["Bash(npm test)"]}}))  # Claude Code's own local approvals
        self.assertEqual(self.ade("init")[0], 0)
        settings, local = json.loads(sp.read_text()), json.loads(lp.read_text())
        self.assertEqual(settings["hooks"], {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "npx expo-hook"}]}]})
        self.assertNotIn("statusLine", settings)
        self.assertEqual(local["permissions"], {"allow": ["Bash(npm test)"]})
        commands = [h["command"] for v in local["hooks"].values() for e in v for h in e["hooks"]]
        self.assertEqual(sorted(commands), sorted(f"{install.ade_cmd()} {verb}" for _, _, verb in install.HOOKS))
        self.assertEqual(self.ade("init")[0], 0)  # a rerun replaces, never doubles
        self.assertEqual(json.loads(lp.read_text()), local)

    def test_hooks_use_the_running_ade_when_it_is_not_on_path(self):
        # The shared Mac: ade lives in /Volumes/ADE/.tools/bin and the shell profile is never edited to add it.
        exe = self.tmp / "tools dir" / "bin" / "ade"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n")
        with mock.patch("shutil.which", return_value=None), mock.patch.object(sys, "argv", [str(exe)]):
            self.assertEqual(install.ade_cmd(), f'"{exe.as_posix()}"')
            self.install()
        self.assertEqual(self.git("status", "--porcelain"), "")  # the machine path never dirties the repo
        lp = self.repo / ".claude/settings.local.json"
        local = json.loads(lp.read_text())
        commands = [h["command"] for v in local["hooks"].values() for e in v for h in e["hooks"]]
        self.assertEqual(sorted(commands), sorted(f'"{exe.as_posix()}" {verb}' for _, _, verb in install.HOOKS))
        self.assertTrue(local["statusLine"]["command"].startswith(f'"{exe.as_posix()}" '))
        # A lane is a worktree of tracked files; it gets this machine's hooks copied in at pick.
        self.ade("card", "add", "note", "--path", "app/calc.py", "--done", "noted")
        ctx = config.load(str(self.repo))
        stages.FUNCS["lane"](ctx, stages.card(ctx, "c1"))
        lane = stages.lane_path(ctx, "c1") / ".claude/settings.local.json"
        self.assertEqual(json.loads(lane.read_text()), local)
        # A different ade first on PATH (a dev checkout ahead of the pinned venv) never stands in for the running kit.
        other = self.tmp / "other kit" / "bin" / "ade"
        other.parent.mkdir(parents=True)
        other.write_text("#!/usr/bin/env python3\nfrom ade.cli import main\n")
        with mock.patch("shutil.which", return_value=str(other)), mock.patch.object(sys, "argv", [str(exe)]):
            self.assertEqual(install.ade_cmd(), f'"{exe.as_posix()}"')
        # Once PATH's ade is this kit, init writes the short form over the path, never beside it.
        mine = Path(install.__file__).resolve().parent.parent / "bin" / "ade"
        with mock.patch("shutil.which", return_value=str(mine)):
            self.assertEqual(install.ade_cmd(), "ade")
            self.install()
        commands = [h["command"] for v in json.loads(lp.read_text())["hooks"].values() for e in v for h in e["hooks"]]
        self.assertEqual(sorted(commands), sorted(f"ade {verb}" for _, _, verb in install.HOOKS))

    def test_settings_rules_are_exactly_ade_toml_and_a_false_key_drops_a_template_rule(self):
        # Guards the contract: permissions in settings.json and the lane settings are what ade.toml owns, no more.
        self.install()
        sp, toml = self.repo / ".claude/settings.json", self.repo / "ade.toml"
        s = json.loads(sp.read_text())
        s["permissions"]["allow"].append("Bash(npm test)")
        sp.write_text(json.dumps(s))
        code, out = self.ade("init", "--no-commit")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Bash(npm test)", json.loads(sp.read_text())["permissions"]["allow"])
        toml.write_text(re.sub(r'(?m)^"Bash\(ade push\*\)" = .*$', '"Bash(ade push*)" = false', toml.read_text()))
        self.assertEqual(self.ade("init", "--no-commit")[0], 0)
        self.assertNotIn("Bash(ade push*)", json.loads(sp.read_text())["permissions"]["allow"])
        lane = json.loads(providers._lane_settings(config.load(self.repo), "c1", str(self.repo)).read_text(encoding="utf-8"))
        self.assertNotIn("Bash(ade push*)", lane["permissions"]["allow"])

    def test_secrets_become_claude_read_and_grep_denies(self):
        # Guards the contract: every [guards] secrets glob is denied to Claude Code's Read and Grep, a rerun changes
        # nothing, and uninstall takes out exactly those rules.
        self.install()
        toml = self.repo / "ade.toml"
        toml.write_text(re.sub(r"(?m)^secrets = \[.*\]$", "secrets = ['.atlas/keys/*']", toml.read_text(), count=1))
        sp = self.repo / ".claude/settings.json"
        self.assertEqual(self.ade("init")[0], 0)
        deny = json.loads(sp.read_text())["permissions"]["deny"]
        for rule in ("Read(~/.atlas/keys/*)", "Grep(~/.atlas/keys/*)", "Read(**/.atlas/keys/*)",
                     "Grep(**/.atlas/keys/*)"):
            self.assertEqual(deny.count(rule), 1)
        before = sp.read_text()
        self.assertEqual(self.ade("init")[0], 0)
        self.assertEqual(sp.read_text(), before)
        self.assertEqual(self.ade("uninstall")[0], 0)
        self.assertNotIn(".atlas/keys", sp.read_text() if sp.exists() else "")

    def test_uninstall_removes_what_init_generated_and_keeps_the_rest(self):
        # The user's own pieces sit beside ade's in every shared file; uninstall must leave exactly those.
        (self.repo / "AGENTS.md").write_text("# Our agents\n\nBe kind.\n")
        (self.repo / ".gitattributes").write_text("*.png binary\n")
        self.install()
        oc, pi = self.repo / ".opencode/plugin/ade.ts", self.repo / ".pi/extensions/ade.ts"
        self.assertFalse(oc.exists() or pi.exists())  # a claude-only project gets neither adapter
        toml = self.repo / "ade.toml"
        toml.write_text(toml.read_text().replace('[roles.builder]\nprovider = "claude"', '[roles.builder]\nprovider = "opencode"'))
        self.assertEqual(self.ade("init")[0], 0)
        self.assertFalse(pi.exists())  # only the engine in use
        self.assertTrue(oc.exists())
        toml.write_text(toml.read_text().replace('provider = "opencode"', 'provider = "claude"'))
        pi.parent.mkdir(parents=True)
        pi.write_text("// mine\n")  # a user's own file is never deleted
        self.assertEqual(self.ade("init")[0], 0)
        self.assertTrue(oc.exists())  # guards a teammate's setup: init never deletes another engine's plugin, only uninstall does
        self.assertEqual(pi.read_text(), "// mine\n")
        pi.unlink()
        toml.write_text(toml.read_text().replace('[roles.builder]\nprovider = "claude"', '[roles.builder]\nprovider = "opencode"'))
        self.assertEqual(self.ade("init")[0], 0)
        sp, lp = self.repo / ".claude/settings.json", self.repo / ".claude/settings.local.json"
        settings = json.loads(sp.read_text())
        settings["permissions"]["allow"].append("Bash(npm test)")
        settings["hooks"] = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "npx expo-hook"}]}]}
        sp.write_text(json.dumps(settings))
        local = json.loads(lp.read_text())
        local["permissions"] = {"allow": ["Bash(ls)"]}
        lp.write_text(json.dumps(local))
        self.ade("card", "add", "note", "--path", "app/calc.py", "--done", "noted")

        code, out = self.ade("uninstall", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.repo / "graph.json").exists())
        code, out = self.ade("uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(sp.read_text()), {"permissions": {"allow": ["Bash(npm test)"]}, "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "npx expo-hook"}]}]}})
        self.assertEqual(json.loads(lp.read_text()), {"permissions": {"allow": ["Bash(ls)"]}})
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "# Our agents\n\nBe kind.\n")
        self.assertEqual((self.repo / ".gitattributes").read_text(), "*.png binary\n")
        for gone in ("graph.json", ".claude/agents/ade-builder.md", ".opencode/plugin/ade.ts", ".pi/extensions/ade.ts"):
            self.assertFalse((self.repo / gone).exists(), gone)
        self.assertTrue((self.repo / "ade.toml").is_file())
        self.assertTrue((self.repo / ".ade/records.jsonl").is_file())
        self.assertEqual(self.ade("uninstall")[0], 0)  # a second uninstall is harmless
        self.assertEqual(self.ade("init")[0], 0)  # and back again
        self.assertTrue((self.repo / "graph.json").is_file())
