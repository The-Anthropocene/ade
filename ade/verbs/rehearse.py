# id: ade.verbs.rehearse
# what: one full loop per config option on throwaway repos with the fake provider; reports calls, wall time and refusals
# cmd: ade rehearse [--only NAME] [--keep] [--coverage]
# reads: the kit itself (run as `python -m ade` in each scenario)
# writes: a temporary folder per run (removed unless --keep); never the current repo, never usage
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import ade

KIT = Path(ade.__file__).resolve().parent.parent


def _swap(old, new):
    def edit(repo):
        p = repo / "ade.toml"
        text = p.read_text(encoding="utf-8")
        if old in text:
            text = text.replace(old, new, 1)
        else:  # a first ade.toml holds only what init chose, so put the line under its section, as the template has it
            tpl = (KIT / "ade" / "template.toml").read_text(encoding="utf-8")
            if old not in tpl:
                raise AssertionError(f"the kit template has no {old!r} to change")
            head = re.findall(r"(?m)^\[[^\]]+\][ \t]*$", tpl[:tpl.index(old)])[-1]
            text = text.replace(head, head + "\n" + new, 1) if head in text else text.rstrip() + f"\n\n{head}\n{new}\n"
        p.write_text(text, encoding="utf-8")
    return edit


def _seed_template(old, new):
    """Make .ade/template.toml an older kit's copy, one default changed."""
    def edit(repo):
        p = repo / ".ade" / "template.toml"
        p.write_text(p.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    return edit


EXISTING = {"calc.py": "# calc: the one module\ndef add(a, b):\n    return a + b\n",
            "test_calc.py": "import unittest\n\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
                            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
            "README.md": "# existing\n"}

LIMITS = ("# id: app.limits\n# what: the limits, read from their data file\n# cmd: -\n# reads: data/limits.json\n# writes: -\n"
          "import json\nfrom pathlib import Path\n\nDATA = Path(__file__).resolve().parent.parent / 'data' / 'limits.json'\n\n\n"
          "def load():\n    return json.loads(DATA.read_text())\n")
CLASH = {"app/__init__.py": "", "app/limits.py": LIMITS, "data/limits.json": '{"max": 5}\n', "tests/__init__.py": "",
         "tests/test_limits.py": "import unittest\n\nfrom app import limits\n\n\nclass T(unittest.TestCase):\n"
                                 "    def test_load(self):\n        self.assertIn('max', limits.load())\n"}
STRICT = ("\n\ndef strict():\n    d = load()\n    if set(d) != {'max'}:\n        raise ValueError(d)\n    return d\n",
          "\n    def test_strict(self):\n        limits.strict()\n")


def _write(files):
    def edit(repo):
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
    return edit


def _foreign(repo):
    """About 300 plain modules, 70 lines each, none with an ADE header: a repo that looks nothing like ADE."""
    body = "".join(f"\n\ndef f{j}(x):\n    y = x + {j}\n    return y * 2\n" for j in range(17))
    _write({f"pkg{i % 10}/mod{i}.py": f'"""Module {i}."""\n' + body for i in range(300)})(repo)


def _question(repo):
    return [r for r in _records(repo) if r["type"] == "decision" and r.get("kind") == "question"][-1]["id"]


def _built(repo, edits):
    """Each card built off the same main on its own branch, as lanes in the air would be."""
    env = {**os.environ, "ADE_PROVIDER": "fake"}
    for card, files in edits.items():
        wt = repo.parent / f"{repo.name}-{card}"
        _git(repo, env, "worktree", "add", "-q", "-b", f"ade/{card}", str(wt), "main")
        _write(files)(wt)
        _ade(wt, env, ["graph"])
        _git(wt, env, "add", "-A")
        _git(wt, env, "commit", "-qm", f"{card}: built in parallel")
        _git(repo, env, "worktree", "remove", "--force", str(wt))


def _parallel(repo):
    """c1 adds a key to the data file, c2 makes the reader insist on exactly the keys it has now. Each is right
    alone; together the reader's own test goes red."""
    _built(repo, {"c1": {"data/limits.json": '{"max": 5, "min": 1}\n'},
                  "c2": {"app/limits.py": LIMITS + STRICT[0], "tests/test_limits.py": CLASH["tests/test_limits.py"] + STRICT[1]}})


def _cut_off(repo):
    """c1 and c2 built; c1's landing is cut off right after its merge (the merge commit, no records), as land's own is."""
    _built(repo, {"c1": {"a.txt": "a\n"}, "c2": {"b.txt": "b\n"}})
    _git(repo, {**os.environ, "ADE_PROVIDER": "fake"}, "merge", "--no-ff", "-q", "-m", "land c1: a", "ade/c1")


def _resumed(repo, env):
    # c2 landed meanwhile; resuming c1 must keep c2's work and add no second merge for c1.
    merges = subprocess.run(["git", "log", "--merges", "--first-parent", "--format=%s", "main"], cwd=repo, env=env,
                            capture_output=True, text=True).stdout.splitlines()
    ok = (repo / "a.txt").exists() and (repo / "b.txt").exists() and sum(m.startswith("land c1") for m in merges) == 1
    return None if ok else f"main lost a landing or merged c1 twice: {merges}"


def _meet(repo):
    """Card c3 built off main, then main moves the same line: the lane's branch now conflicts with trunk."""
    env = {**os.environ, "ADE_PROVIDER": "fake"}
    wt = repo.parent / f"{repo.name}-c3"
    _git(repo, env, "worktree", "add", "-q", "-b", "ade/c3", str(wt), "main")
    (wt / "README.md").write_text("# lane\n", encoding="utf-8")
    _git(wt, env, "commit", "-qam", "c3: built")
    _git(repo, env, "worktree", "remove", "--force", str(wt))
    (repo / "README.md").write_text("# trunk\n", encoding="utf-8")
    _git(repo, env, "commit", "-qam", "trunk moved")


def _redo(repo):
    # The cure for a conflict: the lane starts again from trunk (its work is the fake's one line), then flies.
    env = {**os.environ, "ADE_PROVIDER": "fake"}
    out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo, env=env, capture_output=True, text=True).stdout
    lane = next(l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("worktree ") and l.endswith("/c3"))
    _git(Path(lane), env, "reset", "-q", "--hard", "main")


def _main_green(repo, env):
    # Whatever landed, main itself must pass its whole suite: a clash may stop a card, never break main.
    r = subprocess.run([sys.executable, "-m", "unittest"], cwd=repo, env=env, capture_output=True, text=True)
    return None if r.returncode == 0 else "main is red after both cards: " + (r.stdout + r.stderr).strip().splitlines()[-1]


def _one_parent(repo, env):
    # With [review] fork on, every review forks the day's one parent: the fake wrote the session it was given into each.
    sessions = {line for r in _records(repo) if r["type"] == "review" for line in r.get("text", "").splitlines()
                if line.startswith("fake: session ")}
    return None if len(sessions) == 1 else f"reviews forked {len(sessions)} parents, not one"


def _hint_once(repo, env):
    # An answered question reaches the card's next model call (providers.call), once: the fake echoes what it was told.
    seen = sum(f.read_text().count("-> retry: HINT") for f in (repo / ".ade").glob("*/*.log"))  # lanes/, or logs/ once landed
    return None if seen == 1 else f"the answer 'retry: HINT' reached {seen} model calls, not one"


def _hint_once_dropped(repo, env):
    # The drop answer closed its card (not just its lane), so conduct had nothing to fly.
    bad = _hint_once(repo, env)
    cards = [r for r in _records(repo) if r["type"] == "card" and r.get("status")]
    return bad or (None if cards and cards[-1]["status"] == "closed" else "the dropped card is not closed")


def _old_kit(repo):
    # A launcher pointing at an older kit copy in which one shipped file was edited since: link must list it.
    home = repo.parent / "oldkit-home"
    kit, bin_ = home / "kit", home / ".local" / "bin"
    site = kit / "python" / "lib" / "python3.13" / "site-packages"
    (site / "ade").mkdir(parents=True)
    bin_.mkdir(parents=True)
    (site / "ade" / "old.py").write_text("edited\n")
    (kit / "kit-files.json").write_text(json.dumps({"ade/old.py": hashlib.sha256(b"shipped\n").hexdigest()}))
    if os.name == "nt":  # which() honours PATHEXT there, so the launcher is an ade.cmd, as link writes it
        body = f'@rem ade link: runs the ade install kept off PATH\r\n@"{kit.as_posix()}/ade.cmd" %*\r\n'
        (bin_ / "ade.cmd").write_bytes(body.encode())
    else:
        (bin_ / "ade").write_text(f'#!/bin/sh\n# ade link: runs the ade install kept off PATH\nexec "{kit.as_posix()}/ade" "$@"\n')
        (bin_ / "ade").chmod(0o755)  # which() skips a file it cannot execute, and would find another ade
    return {"HOME": str(home), "USERPROFILE": str(home), "PATH": str(bin_) + os.pathsep + os.environ.get("PATH", "")}


# Each scenario is one config option, walked from nothing to a landed card. A step is argv for `ade` (run in the
# scenario repo), "commit" (commit what changed), or a callable that edits the repo. "start" scenarios begin in an
# empty folder, the newcomer's path; the rest install into a small existing repo.
SCENARIOS = {
    # The starter requires the header: a card naming a file that does not exist yet lands with no hand step (the
    # builder writes the header, the driver regenerates the graph).
    "newcomer-python": {"start": "python", "steps": [["loop", "c1"],
                                                     ["card", "add", "add a fresh module", "--path", "fresh.py",
                                                      "--done", "it exists"], ["loop", "c2"]]},
    "newcomer-typescript": {"start": "typescript", "needs": "node", "steps": [["loop", "c1"]]},
    "install-existing": {"steps": [["init"], "commit", ["card", "add", "note add", "--path", "calc.py", "--done", "noted"],
                                   ["loop", "c1"], {"argv": ["watch", "--once", "c1"], "says": "landed"},
                                   {"argv": ["land", "c1"], "says": "already landed"},
                                   {"argv": ["push"], "says": "nothing pushed"},
                                   ["session", "start", "peek", "--", "status"],
                                   {"argv": ["session", "list"], "says": "peek"}, ["wait", "time", "1s"],
                                   {"argv": ["link", "--dry-run"], "says": "ade"},
                                   {"argv": ["link", "--dry-run"], "env": _old_kit, "says": "ade/old.py"}, ["status"],
                                   ["handover", "--role", "pilot"], ["digest"],
                                   ["telemetry"], {"argv": ["telemetry", "--outcomes"], "says": "landed first time"},
                                   ["graph", "--check"],
                                   {"argv": ["graph", "--route"], "says": "same stop twice"}, ["budget"],
                                   ["decide", "keep calc pure", "--path", "calc.py"],
                                   {"argv": ["decide", "--show", "d1"], "says": "keep calc pure"},
                                   {"argv": ["hook", "brief"], "says": "d1: keep calc pure", "stdin": lambda r: _event(r, "Edit", {"file_path": str(r / "calc.py")})},
                                   {"argv": ["hook", "brief"], "says": "edit.protected", "stdin": lambda r: _event(r, "Edit", {"file_path": str(r / "ade.toml")})},
                                   {"argv": ["hook", "grep"], "says": "ade graph notes", "stdin": lambda r: _event(r, "Grep", {"pattern": "add"}, "calc.py:1:def add")},
                                   {"argv": ["config", "fork"], "says": "fork"}, ["q", "list"]]},
    "prose-only-review-none": {"steps": [["init"], "commit",
                                         ["card", "add", "reword", "--path", "README.md", "--done", "reworded"],
                                         ["loop", "c1"], ["uninstall"], ["init"]]},
    "per-file-tests": {"steps": [["init"], _swap('command = "{python} -m unittest {tests}"',
                                                 'command = "{python} -m unittest {test}"'), "commit",
                                 ["card", "add", "note add", "--path", "calc.py", "--done", "noted"], ["loop", "c1"]]},
    "review-off": {"steps": [["init"], ["parts", "set", "review=off"], "commit",
                             ["card", "add", "note add", "--path", "calc.py", "--done", "noted"], ["loop", "c1"]]},
    "review-fork": {"steps": [["init"], _swap("fork = false", "fork = true"),
                              _write({"CHARTER.md": "# Charter\n\nA small calculator.\n"}), "commit",
                              _seed_template("fork = false", "fork = true"),
                              {"argv": ["init"], "says": "review.fork is the old default true"},
                              ["card", "add", "note add", "--path", "calc.py", "--done", "noted"], ["loop", "c1"],
                              ["card", "add", "note more", "--path", "calc.py", "--done", "noted"], ["loop", "c2"]],
                    "expect": _one_parent},
    "graph-off": {"steps": [["init"], ["parts", "set", "graph=off"], "commit",
                            ["card", "add", "note add", "--path", "calc.py", "--done", "noted"], ["loop", "c1"]]},
    "cockpit-orca-absent": {"steps": [["init"], _swap('mode = "cli"', 'mode = "orca"'), "commit",
                                      ["card", "add", "note add", "--path", "calc.py", "--done", "noted"],
                                      ["loop", "c1"], ["cockpit"]]},
    # Two cards that meet only through a data file no import reaches, built side by side and landed one after the
    # other, with a critical set that does not hold the reader's test. Passing means main stays green.
    "clash-through-data": {"steps": [_write(CLASH), "commit", ["init"],
                                     _swap("critical = []", 'critical = ["test_calc.py"]'), "commit",
                                     ["card", "add", "add a min limit", "--path", "data/limits.json", "--done", "min set"],
                                     ["card", "add", "limits hold exactly max", "--path", "app/limits.py",
                                      "--done", "strict() refuses other keys"],
                                     _parallel, ["loop", "c2"], ["loop", "c1"]],
                           "allow": {3}, "expect": _main_green},
    # S2 offline: a card walked into a protected change, a failing proof and a merge conflict; each step says its
    # stop, and each cure (close, fix main, restart the lane) ends in a landing.
    "stops": {"steps": [["init"], "commit",
                        ["card", "add", "touch the agents file", "--path", "AGENTS.md", "--done", "x"],
                        {"argv": ["loop", "c1"], "says": "guard.protected"},
                        ["card", "close", "c1", "--why", "a person owns that file"],
                        ["card", "add", "note add", "--path", "calc.py", "--done", "noted", "--proof", "test -f proved.txt"],
                        {"argv": ["loop", "c2"], "says": "proof"}, _write({"proved.txt": "ok\n"}), "commit",
                        ["loop", "c2"],
                        ["card", "add", "reword", "--path", "README.md", "--done", "reworded"], _meet,
                        {"argv": ["loop", "c3"], "says": "merge conflict"}, _redo,
                        ["loop", "c3"]], "allow": {3}},  # 3: a loop that stops for a person
    # A landing cut off after its merge, another card landing, then the first resumed: main keeps both, one merge each.
    "resume": {"steps": [["init"], "commit", ["card", "add", "a", "--path", "a.txt", "--done", "x"],
                         ["card", "add", "b", "--path", "b.txt", "--done", "x"], _cut_off, ["loop", "c2"],
                         {"argv": ["loop", "c1"], "says": "landed"}], "expect": _resumed},
    # Two lanes flown side by side land both, and a held card is skipped until its question is answered (draft, below).
    "lanes": {"steps": [["init"], "commit", ["card", "add", "note add", "--path", "calc.py", "--done", "noted"],
                        ["card", "add", "note readme", "--path", "README.md", "--done", "noted"],
                        {"argv": ["conduct", "--lanes", "2"], "says": "c1 landed, c2 landed"}]},
    # S1 offline: a drafted card waits on its accept question, flies once that is answered, and lands untouched.
    # A step may be a dict: its argv (or a callable giving argv from the repo), and a line its output must say.
    "foreign-repo": {"steps": [_foreign, ["init"], "commit",
                               ["card", "add", "touch one module", "--path", "pkg3/mod3.py", "--done", "touched"],
                               {"argv": ["loop", "c1"], "lacks": ["docs/testing.md", "rehearse --coverage", "site/docs/"]}]},
    "draft": {"steps": [["init"], "commit", ["card", "draft", "note something"],
                        {"argv": ["conduct"], "says": "nothing open"},
                        {"argv": lambda repo: ["decide", "--answer", _question(repo), "retry: HINT"]},
                        ["conduct"], ["card", "draft", "note another"],
                        {"argv": ["decide", "--desk"], "stdin": "bogus\ns\nq\n", "says": "accepted answers"},
                        {"argv": lambda repo: ["decide", "--answer", _question(repo), "drop"], "says": "closed"},
                        {"argv": ["conduct"], "says": "nothing open"}], "expect": _hint_once_dropped},
}


def unflown():
    """(verbs, parts) no scenario flies: the CLI's own verb list and parts catalogue less what the scenarios' steps name."""
    from ade import cli, parts
    flown, on = {"rehearse", "init"}, set()  # rehearse is the runner; every "start" scenario opens with init
    for spec in SCENARIOS.values():
        for s in spec["steps"]:
            argv = s["argv"] if isinstance(s, dict) else s
            if isinstance(argv, list) and argv:
                flown.add(argv[0])
                if argv[:2] == ["parts", "set"]:
                    on.update(a.split("=")[0] for a in argv[2:])
    return sorted(set(cli.verbs()) - flown), sorted(set(parts.catalogue()) - on)


def add(p):
    p.add_argument("--coverage", action="store_true", help="list the verbs and parts switches no scenario runs, then stop")
    p.add_argument("--only", choices=sorted(SCENARIOS), action="append", help="run just this scenario (repeatable)")
    p.add_argument("--keep", action="store_true", help="keep the scenario folders and print where they are")


def _env(base):
    gitcfg = base / "gitconfig"
    gitcfg.write_text("[user]\n\tname = ADE Rehearsal\n\temail = rehearse@example.invalid\n"
                      "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    # The fake provider for every role, lanes inside the run folder, and no git config of the machine's.
    return {**os.environ, "ADE_PROVIDER": "fake", "ADE_WORK": str(base / "work"), "GIT_CONFIG_GLOBAL": str(gitcfg),
            "GIT_CONFIG_NOSYSTEM": "1", "PYTHONPATH": os.pathsep.join(filter(None, [str(KIT), os.environ.get("PYTHONPATH")])),
            "ORCA_CLI_COMMAND": "ade-rehearsal-no-orca"}


def _event(repo, tool, tool_input, out=""):
    return json.dumps({"cwd": str(repo), "session_id": "rehearsal", "tool_name": tool,
                       "tool_input": tool_input, "tool_response": {"stdout": out}})


def _ade(repo, env, argv, stdin=None):
    return subprocess.run([sys.executable, "-m", "ade", *argv], cwd=repo, env=env, capture_output=True, text=True, input=stdin,
                          encoding="utf-8", errors="replace", timeout=600)


def _git(repo, env, *args):
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _records(repo):
    p = repo / ".ade" / "records.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.is_file() else []


def scenario(name, spec, base, env):
    """Walk one scenario; the checkpoint after each step records its exit code, wall time and any refusal codes."""
    repo = base / name
    repo.mkdir()
    if spec.get("needs") and not shutil.which(spec["needs"]):
        return {"name": name, "result": "skip", "why": f"{spec['needs']} is not on PATH", "checkpoints": []}
    if spec.get("start"):
        steps = [["init", "--starter", spec["start"]], *spec["steps"]]
    else:
        for rel, text in EXISTING.items():
            (repo / rel).write_text(text, encoding="utf-8")
        _git(repo, env, "init", "-q", "-b", "main")
        _git(repo, env, "add", "-A")
        _git(repo, env, "commit", "-qm", "an existing project")
        steps = spec["steps"]
    checkpoints, started = [], time.monotonic()
    for step in steps:
        t = time.monotonic()
        if step == "commit":
            _git(repo, env, "add", "-A")
            if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo, env=env).returncode:
                _git(repo, env, "commit", "-qm", "rehearsal step")
            continue
        if callable(step):
            step(repo)
            continue
        says, stdin = (step.get("says"), step.get("stdin")) if isinstance(step, dict) else (None, None)
        lacks = step.get("lacks", ()) if isinstance(step, dict) else ()
        extra = step["env"](repo) if isinstance(step, dict) and step.get("env") else {}
        if isinstance(step, dict):
            step = step["argv"](repo) if callable(step["argv"]) else step["argv"]
        r = _ade(repo, {**env, **extra}, step, stdin(repo) if callable(stdin) else stdin)
        rc = 0 if r.returncode in spec.get("allow", ()) else r.returncode
        out = r.stdout + r.stderr
        rc = rc or (0 if not says or says in out else 1)  # a step may also demand a line of its output
        rc = rc or int(any(t in out for t in lacks))  # and text its output must not hold
        codes = sorted({w.strip("[]") for w in out.split() if w.startswith("[") and "." in w and w.endswith("]")})
        checkpoints.append({"step": "ade " + " ".join(step), "rc": rc, "wall_s": round(time.monotonic() - t, 2),
                            "refusals": codes, "tail": out.strip().splitlines()[-3:]})
        if rc:
            break
    rows = _records(repo)
    landed = any(r["type"] == "card" and r.get("status") == "landed" for r in rows)
    ok = landed and all(c["rc"] == 0 for c in checkpoints)
    why = spec["expect"](repo, env) if spec.get("expect") else None
    if why:
        checkpoints.append({"step": "expect", "rc": 1, "wall_s": 0, "refusals": [], "tail": [why]})
    return {"name": name, "result": "pass" if ok and not why else "fail", "landed": landed,
            "calls": sum(r["type"] == "usage" for r in rows), "wall_s": round(time.monotonic() - started, 2),
            "checkpoints": checkpoints}


def run(args, cwd):
    if args.coverage:
        verbs, switches = unflown()
        print("verbs no scenario runs: " + (", ".join(verbs) or "none"))
        print("parts no scenario switches: " + (", ".join(switches) or "none"))
        return 0
    base = Path(tempfile.mkdtemp(prefix="ade-rehearse-")).resolve()
    env = _env(base)
    results = []
    try:
        for name in args.only or SCENARIOS:
            res = scenario(name, SCENARIOS[name], base, env)
            results.append(res)
            extra = res.get("why") or f"{res['calls']} calls, {res['wall_s']}s"
            print(f"{res['result']:<5} {name:<24} {extra}")
            if res["result"] == "fail":
                for c in res["checkpoints"]:
                    print(f"      {c['step']}: rc {c['rc']} {' '.join(c['refusals'])}\n        " + "\n        ".join(c["tail"]))
        (base / "rehearsal.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    finally:
        if args.keep:
            print(f"kept: {base} (rehearsal.json holds every checkpoint)")
        else:
            shutil.rmtree(base, ignore_errors=True)
    failed = [r["name"] for r in results if r["result"] == "fail"]
    print(f"rehearsal: {len(results) - len(failed)} of {len(results)} ok"
          + (f"; failed: {', '.join(failed)}" if failed else "") + "; no usage spent (fake provider)")
    return 1 if failed else 0
