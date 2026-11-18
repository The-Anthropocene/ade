# id: tests.test_guard
# what: hooks 2 to 4: the bash guard refuses its named classes and nothing else; turn-end records holds; start hands over
# cmd: python3 -m unittest tests/test_guard.py
# reads: tests/fixture_repo
# writes: temporary directories only
import ast
import json
import os
import re
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import ade
from ade import cli, config, guard, install, records
from ade.refuse import Refusal
from ade.verbs import handover, hook  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase

ALLOWED = ["git clean -fd", "bash -c 'git status'", "rm -rf .github/old", "bash -lc 'git status'", "rm .gitignore.bak",
           "git status", "cat README.md", "rm -rf build", "rm -rf src/*", "cp .env.example .env", 'cmd | grep -v ".env"', "cat x | grep -v .env", 'grep -n "*.key" src/app.py', "awk '/.env/' README.md",
           "docker run --env-file .env app", "git push origin main", "cd app && cat a.py", "git -C . status", "cat ade.toml", "cat .env.example",
           "git rebase main", "echo `date`", 'echo "$(date)"', "eval true", "python -c 'print(1)' | head", "git reset --hard",
           "Remove-Item build/out.txt", "Set-Content notes.txt x", "Get-Content README.md"]
REFUSED = {
    "guard.secret": ["cat .env", "ls && cat .env.local", "grep KEY config/.env", "FOO=1 cat .env", "head id_rsa", "grep x .env", "grep -e x .env", "grep -- -v .env", "rg -- -x .env", "sed -n p .env", "awk -f p.awk .env",
                     "bash -c 'cat .env'", "sh -c \"ls; cat .env\"", "pwsh -Command 'Get-Content .env'",
                     "bash -lc 'cat .env'", "sh -ec 'cat .env'", "x=`cat .env`", 'eval "cat .env"', 'echo "$(cat .env)"'],
    "guard.destructive": ["rm -rf /", "rm -rf ~", "rm -fr .", "sudo rm -rf *", "rm -rf /usr/other", "rm -r -f ./",
                          "rm -rf .git", "rm -rf ./.git/", "rm -rf .ade", "git clean -fdx", "git clean -X -f",
                          "rm -rf $(pwd)", "rm -rf `pwd`", "rm -rf $DIR", "rm -f .ade/records.jsonl",
                          "mv .ade /tmp/x", "rm .git/index", "Remove-Item -Recurse -Force .git", "ri -r -fo .ade",
                          "del /s /q .git", "cd / && rm -rf home", "rd /s /q C:/", "Remove-Item -Recurse -Force /"],
    "guard.history": ["git push --force", "git push -f origin x", "git push origin +main", "git filter-branch x",
                      "git push --force-with-lease", "git push origin --delete main", "git push origin :main",
                      "git branch -D main", "git branch -d main", "git update-ref -d refs/heads/main", "git tag -d v1",
                      "git switch main && git reset --hard X", "git checkout main; git reset --hard X",
                      "git -C . push --force origin main", "git -c a=b push -f", "git --no-pager push --force", "git --git-dir=.git push -f"],
    "guard.protected": ["echo x > ade.toml", "echo x>ade.toml", "sed -i s/a/b/ .claude/settings.json",
                        "tee -a AGENTS.md", "rm .claude/agents/ade-pilot.md",
                        "Set-Content ade.toml x", "sc -Path ade.toml -Value x", "Add-Content ade.toml x",
                        "x | Out-File -FilePath ade.toml", "powershell -c 'Set-Content ade.toml x'",
                        "pwsh.exe -COMMAND 'Set-Content ade.toml x'", "Write-Output x > ade.toml", "cd .claude && echo {} > settings.local.json"],
}


class GuardTest(FixtureCase):
    def code(self, command, branch="ade/c1"):
        try:
            guard.check(config.load_at(self.repo).cfg, command, str(self.repo), str(self.repo), branch)
        except Refusal as r:
            self.assertTrue(r.cure)
            return r.code
        return None

    def test_named_classes_refused_everything_else_allowed(self):
        # Guards the bash guard's contract: each named class (secret, destructive, history, protected) is refused with a cure, nothing else is.
        for command in ALLOWED:
            self.assertIsNone(self.code(command), command)
        for code, commands in REFUSED.items():
            for command in commands:
                self.assertEqual(self.code(command), code, command)
        # [guards] secrets globs apply to the whole repo- or home-relative path, not only the basename.
        cfg = config.load_at(self.repo).cfg
        cfg["guards"]["secrets"] = cfg["guards"]["secrets"] + ["*/keys/*"]
        with self.assertRaises(Refusal) as r:
            guard.check(cfg, "cat ~/x/keys/a.txt", str(self.repo), str(self.repo), "ade/c1")
        self.assertEqual(r.exception.code, "guard.secret")
        # A relative glob refuses the file wherever it is named, not only under the repo or home.
        cfg["guards"]["secrets"] = cfg["guards"]["secrets"] + ["site-auth/*"]
        with self.assertRaises(Refusal) as r:
            guard.check(cfg, "cat /Volumes/x/clone/site-auth/k", str(self.repo), str(self.repo), "ade/c1")
        self.assertEqual(r.exception.code, "guard.secret")
        guard.check(cfg, "cat /Volumes/x/clone/README.md", str(self.repo), str(self.repo), "ade/c1")
        # Guards: a 'tracked:' secret_ok glob allows a file back only while git tracks it; an untracked one stays a secret.
        (self.repo / "site-auth").mkdir(exist_ok=True)
        for n in ("app.py", "new.py"):
            (self.repo / "site-auth" / n).write_text("x")
        self.git("add", "site-auth/app.py")
        guard.tracked.cache_clear()
        cfg["guards"]["secret_ok"] = ["tracked:site-auth/*"]
        guard.check(cfg, "cat site-auth/app.py", str(self.repo), str(self.repo), "ade/c1")
        with self.assertRaises(Refusal) as r:
            guard.check(cfg, "cat site-auth/new.py", str(self.repo), str(self.repo), "ade/c1")
        self.assertEqual(r.exception.code, "guard.secret")
        # Guards: a directory-wide Grep whose glob names a secret is refused; the static deny keeps non-overlapping globs.
        ctx = types.SimpleNamespace(cfg=cfg)
        with self.assertRaises(Refusal) as r:
            hook._read(ctx, {"tool_input": {"path": ".", "glob": ".env"}}, str(self.repo), self.repo)
        self.assertEqual(r.exception.code, "guard.secret")
        self.assertIn("Read(**/.env)", install._denied(ctx))
        self.assertNotIn("Read(**/site-auth/*)", install._denied(ctx))
        cfg["guards"]["secret_ok"] = []
        # A [protected] cures entry replaces the default cure, for a shell write as for an edit.
        cfg["protected"]["paths"] = cfg["protected"]["paths"] + ["engine/v1/*"]
        cfg["protected"]["cures"] = {"engine/v1/*": "v1 is frozen; build it in v2/"}
        with self.assertRaises(Refusal) as r:
            guard.check(cfg, "echo x > engine/v1/a.py", str(self.repo), str(self.repo), "ade/c1")
        self.assertEqual(r.exception.cure, "v1 is frozen; build it in v2/")
        # Rewriting a commit is only history on main; in a lane it is ordinary work.
        self.assertEqual(self.code("git commit --amend", branch="main"), "guard.history")
        self.assertEqual(self.code("git reset --hard HEAD~1", branch="main"), "guard.history")
        self.assertIsNone(self.code("git commit --amend"))
        # A recursive delete strictly inside the system temp folder is a builder's own scratch; the folder itself is not.
        tmp = tempfile.gettempdir().replace("\\", "/")  # what a Windows Bash user types
        self.assertIsNone(self.code(f'rm -rf "{tmp}/scratch"'))
        self.assertEqual(self.code(f'rm -rf "{tmp}"'), "guard.destructive")
        self.assertEqual(self.code(f'rm -rf "{tmp}"/*'), "guard.destructive")
        self.assertEqual(self.code(f'rm -rf "{tmp}"/../../x'), "guard.destructive")
        with mock.patch.dict(os.environ, {"TMPDIR": tmp}):
            self.assertIsNone(self.code("rm -rf ${TMPDIR}/a/b"))
            self.assertEqual(self.code("rm -rf $TMPDIR"), "guard.destructive")
            self.assertEqual(self.code("rm -rf $OTHER/a"), "guard.destructive")
        # Commands run through the output store are judged as if run directly.
        self.assertEqual(self.code("ade q run -- rm -rf /"), "guard.destructive")
        self.assertEqual(self.code("ade q run --card c1 -- 'git push --force'"), "guard.history")
        self.assertIsNone(self.code("ade q run -- python3 -m unittest"))
        # Also guards: a drive (C:/x) or UNC (//host/share) token is never joined onto cwd, so absolute globs match it
        # on every platform.
        cfg = config.load_at(self.repo).cfg
        cfg["guards"]["secrets"] = ["C:/Users/*/.ssh/*", "//srv/keys/*"]
        for tok in ("C:/Users/me/.ssh/config", "//srv/keys/a"):
            with self.assertRaises(Refusal) as r:
                guard.check(cfg, "cat " + tok, "C:/work/repo", "C:/work/repo", "ade/c1")
            self.assertEqual(r.exception.code, "guard.secret")
        # A repo path named absolutely, in either drive case, is still seen as repo-relative.
        for tok in ("C:/work/repo/ade.toml", "c:/work/repo/ade.toml"):
            self.assertEqual(guard._rel(tok, "C:/work/repo", "C:/work/repo"), "ade.toml")

    def hook(self, name, **event):
        return self.ade("hook", name, stdin=json.dumps({"session_id": "s1", "cwd": str(self.repo), **event}))

    def test_bash_hook_denies_with_the_cure(self):
        self.install()
        code, out = self.hook("bash", tool_input={"command": "cat .env"})
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("cure:", decision["permissionDecisionReason"])
        self.assertEqual(self.hook("bash", tool_input={"command": "git status"})[1], "")

    def test_refusal_from_a_lane_cwd_lands_in_the_main_checkouts_records(self):
        # Guards: a hook refusal run with a lane (worktree) as cwd writes its one refusal row to the main
        # checkout's records, never to a lane-local .ade.
        self.install()
        lane = self.repo.parent / "lane-c271"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "ade/c271x", str(lane)], cwd=self.repo, check=True)
        self.addCleanup(subprocess.run, ["git", "worktree", "remove", "--force", str(lane)], cwd=self.repo)
        count = 0
        for name, tool_input in (("bash", {"command": "cat .env"}), ("brief", {"file_path": "ade.toml"})):
            event = {"session_id": "s1", "cwd": str(lane), "tool_input": tool_input}
            code, out = self.ade("hook", name, stdin=json.dumps(event))
            self.assertIn("deny", out)
            count += 1
            rows = records.read(config.load_at(self.repo), "refusal")
            self.assertEqual(len(rows), count, name)
        self.assertFalse((lane / ".ade/records.jsonl").exists())

    def test_turn_end_records_one_hold_per_stop_and_start_hands_over(self):
        self.install()
        code, out = self.ade("card", "add", "note", "--path", "app/calc.py", "--done", "noted")
        calc = self.repo / "app/calc.py"
        calc.write_text(calc.read_text() + "# dirty main\n")
        self.assertEqual(self.ade("loop", "c1")[0], 3)  # waits: the dirty main checkout shares a path with the lane
        self.hook("turn-end")
        self.hook("turn-end")
        rows = [json.loads(x) for x in (self.repo / ".ade/records.jsonl").read_text().splitlines()]
        holds = [r for r in rows if r["type"] == "hold"]
        self.assertEqual([(h["card"], h["kind"]) for h in holds], [("c1", "machine")])
        context = json.loads(self.hook("start", source="startup")[1])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("# ADE handover: pilot", context)
        self.assertIn("c1 note", context)
        # Guards the window: an agent-answered question for a person is listed only while its row is under 7 days old.
        ctx = config.load_at(self.repo)
        for i, age in (("d901", 30), ("d902", 1)):
            records.append(ctx, "decision", {"id": i, "kind": "question", "text": "q", "paths": [], "answer": "ok", "by": "agent"})
            path = self.repo / ".ade/records.jsonl"
            last = json.loads(path.read_text().splitlines()[-1])
            last["at"] = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat(timespec="seconds")
            path.write_text("".join(x + "\n" for x in path.read_text().splitlines()[:-1]) + json.dumps(last) + "\n")
        context = json.loads(self.hook("start", source="startup")[1])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("d902: answered by an agent", context)
        self.assertNotIn("d901: answered by an agent", context)
        # Guards: a lane's resume (forked review, retry) adds no second handover; its compact and the pilot's resume do.
        with mock.patch.dict(os.environ, {"ADE_ROLE": "builder"}):
            self.assertEqual(self.hook("start", source="resume")[1], "")
            self.assertIn("# ADE handover: builder", self.hook("start", source="compact")[1])
        self.assertIn("# ADE handover: pilot", self.hook("start", source="resume")[1])

    def test_briefing_reuse_line_and_headerless_file_description(self):
        # Guards the reuse line's contract: a blessed helper a neighbour imports is named, an unblessed one never is.
        self.install()
        (self.repo / "lib").mkdir()
        (self.repo / "lib/util.py").write_text("def tidy(x):\n    return x\n")
        imports = {"app/a.py": ["lib/util.py"], "app/b.py": ["lib/util.py", "app/a.py"], "app/c.py": []}
        graph = {"files": {n: {"imports": i} for n, i in imports.items()}, "callers": {}, "tests": {}}
        ctx = config.load_at(self.repo)
        with mock.patch("ade.graph.load", return_value=graph):
            self.assertNotIn("reuse:", hook.briefing(ctx, "app/c.py"))
            ctx.cfg["budgets"].setdefault("components", {})["lib"] = {"globs": ["lib/*"]}
            self.assertIn("reuse: lib/util.py:tidy (used by 2 files)", hook.briefing(ctx, "app/c.py"))
        # Also guards that [graph] blessed alone (a string counts as one glob) drives the reuse line.
        graph = {"files": {"app/a.py": {"imports": ["lib/util.py"]}, "app/c.py": {}}, "callers": {}, "tests": {}}
        ctx = config.load_at(self.repo)
        for blessed in ("lib/*", ["lib/*"]):
            ctx.cfg["graph"]["blessed"] = blessed
            with mock.patch("ade.graph.load", return_value=graph):
                self.assertIn("reuse: lib/util.py:tidy", hook.briefing(ctx, "app/c.py"))
        # Also guards that the helper's function is found by the graph's lang for it: TypeScript names its first export.
        (self.repo / "src/lib").mkdir(parents=True)
        (self.repo / "src/lib/x.ts").write_text("import y from './y'\nexport async function tidy(x) {}\n")
        graph = {"files": {"src/a.ts": {"imports": ["src/lib/x.ts"]}, "src/c.ts": {}, "src/lib/x.ts": {"lang": "ts"}},
                 "callers": {}, "tests": {}}
        ctx = config.load_at(self.repo)
        ctx.cfg["graph"]["blessed"] = "src/lib/*"
        with mock.patch("ade.graph.load", return_value=graph):
            self.assertIn("reuse: src/lib/x.ts:tidy", hook.briefing(ctx, "src/c.ts"))
        # Also guards that a headerless prose file is described by its title, while a headerless code file is not.
        (self.repo / "notes.md").write_text("intro\n# Release notes\n# Later\n")
        (self.repo / "app/bare.py").write_text("x = 1\n")
        graph = {"files": {"notes.md": {}, "app/bare.py": {}}, "callers": {}, "tests": {}}
        ctx = config.load_at(self.repo)
        with mock.patch("ade.graph.load", return_value=graph):
            self.assertIn("): Release notes\n", hook.briefing(ctx, "notes.md"))
            self.assertIn("): no header\n", hook.briefing(ctx, "app/bare.py"))

    def test_briefing_drops_a_park_question_once_its_card_has_landed_but_keeps_rulings(self):
        # Guards the briefing contract: only a park question of a landed card is settled; rulings keep governing,
        # even one that names the landed card.
        self.install()
        ctx = config.load_at(self.repo)
        graph = {"files": {}, "callers": {}, "tests": {}}
        records.append(ctx, "decision", {"text": "ruling text", "paths": ["app/*"], "kind": "ruling", "card": "c9"},
                       prefix="d")
        records.append(ctx, "card", {"id": "c9", "status": "open"})
        records.append(ctx, "decision", {"text": "parked text", "paths": ["app/*"], "kind": "question", "card": "c9",
                                         "rec": "retry", "default": "retry", "deadline": "2999-01-01"},
                       prefix="d")
        with mock.patch("ade.graph.load", return_value=graph):
            out = hook.briefing(ctx, "app/a.py")
            self.assertIn("ruling text", out)
            self.assertIn("parked text", out)
            records.append(ctx, "card", {"id": "c9", "status": "landed"})
            out = hook.briefing(ctx, "app/a.py")
            self.assertIn("ruling text", out)
            self.assertNotIn("parked text", out)
        # Guards the hook contract: a Bash write fires a file's ruling once per session, sharing Edit's seen file.
        with mock.patch("ade.graph.load", return_value=graph):
            ev = {"tool_input": {"command": "echo x > app/a.py"}}
            first = json.loads(self.hook("bash", **ev)[1])["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(first.count("RULES for app/a.py"), 1)
            self.assertEqual(self.hook("bash", **ev)[1], "")
            self.assertEqual(self.hook("brief", tool_input={"file_path": "app/a.py"})[1], "")

    def test_briefing_keeps_the_most_specific_ruling_inside_a_small_cap(self):
        # Guards the ordering contract: rulings lead the brief, most specific glob first, so the one that
        # names the file exactly survives a tight character cap however many broader rulings overlap it.
        self.install()
        ctx = config.load_at(self.repo)
        broad = ["*", "app/*", "app/*.py", "app/a*", "app/?.py", "app/**", "**/a.py", "app/[a-z].py", "*/*.py"]
        for g in broad:
            records.append(ctx, "decision", {"text": f"broad {g}. More.", "paths": [g], "kind": "ruling"}, prefix="d")
        records.append(ctx, "decision", {"text": "exact one. More.", "paths": ["app/a.py"], "kind": "ruling"}, prefix="d")
        for g in broad:  # newer rulings follow the exact one, so only specificity can keep it first
            records.append(ctx, "decision", {"text": f"newer {g}. More.", "paths": [g], "kind": "ruling"}, prefix="d")
        ctx.cfg["budgets"]["brief_tokens"] = 250
        with mock.patch("ade.graph.load", return_value={"files": {}, "callers": {}, "tests": {}}):
            out = hook.briefing(ctx, "app/a.py")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("RULES for app/a.py (follow these; ade decide --show dN for the full text):"))
        self.assertIn("exact one", lines[1])
        self.assertEqual(len([l for l in lines if l.startswith("d")]), 6)
        self.assertEqual(out.count("ade decide --show"), 1)
        self.assertLessEqual(len(out.split("the full text):\n", 1)[1]), 1500)
        long = "x" * 286 + " one sentence."  # 300 characters: shown whole, not cut at 140
        records.append(ctx, "decision", {"text": long, "paths": ["app/a.py"], "kind": "ruling"}, prefix="d")
        with mock.patch("ade.graph.load", return_value={"files": {}, "callers": {}, "tests": {}}):
            self.assertIn(": " + long, hook.briefing(ctx, "app/a.py"))


class CureVerbsTest(unittest.TestCase):
    # Guards the contract that a printed cure only names verbs the CLI has: it reads every cure given to
    # refuse() and every Result next-line from the source and checks each backticked `ade <verb>`.
    def test_every_cure_names_a_real_verb(self):
        known = set(cli.verbs())
        bad = []
        for p in Path(ade.__file__).parent.rglob("*.py"):
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", getattr(node.func, "attr", None))
                if name == "refuse":
                    parts = node.args[2:3] + [k.value for k in node.keywords if k.arg == "cure"]
                elif name == "Result":
                    parts = node.args[2:3] + [k.value for k in node.keywords if k.arg == "next"]
                else:
                    continue
                for part in parts:
                    for s in ast.walk(part):
                        if isinstance(s, ast.Constant) and isinstance(s.value, str):
                            bad += [f"{p.name}: {v}" for v in re.findall(r"`ade ([\w-]+)", s.value) if v not in known]
        self.assertEqual(bad, [])
