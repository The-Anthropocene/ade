# id: ade.stages.test
# what: test stage: always-run checks, then graph-selected tests; one retry if timing-shaped, reds also on main are carried
# cmd: -
# reads: .ade/records.jsonl, lane worktree, graph
# writes: .ade/records.jsonl (test, test_red: card, test, touched)
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from ade import graph, host, parts, push, q, records
from ade.gitx import git
from ade.refuse import refuse
from ade.stages import build, lane
from ade.stages import Result, cached, lane_path, main, touched, tree


def split(cmd):
    return host.substitute(shlex.split(cmd), python=host.PYTHON)


def values(ctx, wt, base=None):
    """{paths}, {base} and {tip} for the tree at wt: the files changed since base (default main), base, and wt's HEAD."""
    base = base or git(ctx.root, "rev-parse", main(ctx))
    tip = git(wt, "rev-parse", "HEAD")
    return {"paths": git(wt, "diff", "--name-only", f"{base}...{tip}").splitlines(), "base": base, "tip": tip}


def unit_map(ctx, root, paths, brief=False):
    """(units, note) from [tests] map: the project's own runner names units per path. The output is cached per commit
    and paths under .ade; a failing or non-JSON map gives no units and a one-line note, so the graph stands alone.
    brief (the first-edit hook) runs a cache miss under a few seconds, never the tests timeout; a failure is silent."""
    cmd = ctx.cfg["tests"].get("map") or ""
    if not cmd or not paths:
        return [], ""
    key = hashlib.sha1(json.dumps([git(root, "rev-parse", "HEAD"), cmd, sorted(paths)]).encode()).hexdigest()
    cache = ctx.state / "testmap" / (key + ".json")
    try:
        if cache.is_file():
            got = json.loads(host.read(cache))
        else:
            argv = host.substitute(split(cmd), paths=list(paths))
            r = subprocess.run(host.which(argv), cwd=root, capture_output=True, text=True,
                               timeout=3 if brief else ctx.cfg["tests"]["timeout"])
            if r.returncode:
                raise ValueError(f"exit {r.returncode}")
            got = json.loads(r.stdout)
            if not isinstance(got, dict):
                raise ValueError("not a JSON object")
            cache.parent.mkdir(parents=True, exist_ok=True)
            host.write(cache, json.dumps(got))
        bad = [p for p in paths if p in got
               and not (isinstance(got[p], list) and all(isinstance(u, str) for u in got[p]))]
        units = sorted({u for p in paths if p not in bad for u in got.get(p, [])})
        return units, (f"[tests] map ignored a value that is not a list of strings ({bad[0]})" if bad else "")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return [], "" if brief else f"[tests] map failed ({str(e)[:80]}); the graph selects alone"


def commands(cfg, g, chosen):
    """Every argv the stage runs: [tests].always first, then {tests} once with every selected file, or {test}
    once per file. chosen None means the whole suite: every graph test file (not __init__.py) goes to {tests} or {test},
    so a tests/ folder without __init__.py still runs."""
    runs = [split(cmd) for cmd in cfg["tests"]["always"]]
    base = split(cfg["tests"]["command"])
    # A package's __init__.py matches a test glob but is not a test to run.
    each = chosen if chosen is not None else sorted(
        n for n, f in g["files"].items() if f["test"] and not n.endswith("__init__.py"))
    if "{test}" in base:
        runs += [host.substitute(base, test=[f]) for f in each]
    else:
        runs.append(host.substitute(base, tests=each))
    return runs  # {paths}, {base} and {tip} stay as tokens; host.substitute fills them from values()


def _run(ctx, argv, cwd):
    limit = ctx.cfg["tests"]["timeout"]
    try:
        r = subprocess.run(host.which(argv), cwd=cwd, capture_output=True, timeout=limit)
    except OSError as e:
        # A test command that cannot start is a red run that names it, not a traceback.
        return 127, f"{shlex.join(argv)} could not start: {e}\n"
    except subprocess.TimeoutExpired as e:
        # A hang is a red run with the command named, not a crash of the loop.
        got = [x.decode(errors="replace") if isinstance(x, bytes) else x or "" for x in (e.stdout, e.stderr)]
        return 124, "".join(got) + f"\nthe run passed the tests timeout of {limit} seconds: {shlex.join(argv)}\n"
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", errors="replace")  # bytes that are not UTF-8 become U+FFFD


EMPTY = re.compile(r"Ran 0 tests|NO TESTS RAN|no tests ran|collected 0 items", re.I)


def empty_files(ctx, lp, g, vals):
    """The test files the lane adds or changes (not __init__.py, not a helper another file imports) in which the [tests] command, run on that file alone, finds no tests."""
    found = []
    for f in vals["paths"]:
        if not (g["files"].get(f, {}).get("test") and not f.endswith("__init__.py") and (lp / f).is_file()):
            continue
        if g.get("callers", {}).get(f):
            continue  # a shared helper another file imports holds no tests of its own
        code, out = _run(ctx, host.substitute(commands(ctx.cfg, g, [f])[-1], **vals), lp)
        if code == 5 or EMPTY.search(out):
            found.append(f)
    return found


def _tail(argv, out):
    return f"$ {shlex.join(argv)}\n{out[-2000:]}"


def red_names(out):
    found = re.findall(r"^(?:FAIL|ERROR): (?:\S+ \(([^)\s]+)\)|(\S+))|^FAILED (\S+::\S+)", out, re.M)
    return list(dict.fromkeys(a or b or c for a, b, c in found))


def note_reds(ctx, c, argv, out, g, paths):
    """One test_red row per red test (the command when its output names none): card, test, and whether the lane's
    diff touched the test's subject, meaning the test file or anything it imports. A red we cannot place counts as touched."""
    files = g.get("files", {})
    for name in red_names(out) or [shlex.join(argv)]:
        head = name.split("::")[0]
        bits = [head] if "::" in name else head.split(".")
        f = next((x for x in ("/".join(bits[:n]) + ("" if bits[0].endswith(".py") else ".py") for n in range(len(bits), 0, -1))
                  if x in files), None)
        subject = {f, *files[f].get("imports", [])} if f else None
        records.append(ctx, "test_red", {"card": c["id"], "test": name,
                                         "touched": True if subject is None else bool(subject & set(paths))})


def failed_names(argv, out):
    """Up to three test ids from a failing command's own failure lines (unittest FAIL/ERROR, pytest FAILED), with
    '+N more'; the command itself when its output has none."""
    names = red_names(out)
    if not names:
        return shlex.join(argv)
    return ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")


def fingerprint(out, *roots):
    # The failure with its worktree paths and timings taken out, so the same red reads the same in a lane and on main.
    for r in roots:
        out = out.replace(str(r), "<root>").replace(Path(r).as_posix(), "<root>")
    out = re.sub(r"\d+(\.\d+)?\s*(ms|s|sec|seconds)\b", "<t>", out)
    return hashlib.sha1(re.sub(r"0x[0-9a-fA-F]+", "<addr>", out).encode()).hexdigest()


def red_on_main(ctx, argv, lp, lane_out, template=None):
    """True only when the same command fails on main with the same output (paths and timings aside). A lane
    that adds new failures to a command main already fails reads differently, and blocks."""
    mtree = git(ctx.root, "rev-parse", f"{main(ctx)}^{{tree}}")
    template = template or argv
    key = shlex.join(template)
    mine = fingerprint(lane_out, lp)
    for r in records.read(ctx, "test"):
        if r["card"] == "main" and r["tree"] == mtree and r["tests"] == key:
            return r["result"] == "fail" and r.get("fingerprint") == mine
    mp = lane_path(ctx, "_main")
    if (mp / ".git").exists():
        git(mp, "checkout", "-q", "--detach", main(ctx))
    else:
        mp.parent.mkdir(parents=True, exist_ok=True)
        git(ctx.root, "worktree", "add", "-q", "--detach", str(mp), main(ctx))
    lane.share(ctx, mp)
    msha = git(ctx.root, "rev-parse", main(ctx))
    margv = host.substitute(template, paths=[], base=msha, tip=msha)  # main's own shas, nothing changed
    if any((lp / a).is_file() and not (mp / a).is_file() for a in margv):
        return False  # it names a file main does not have, so main cannot vouch for it
    code, out = _run(ctx, margv, mp)
    theirs = fingerprint(out, mp)
    records.append(ctx, "test", {"card": "main", "tree": mtree, "tests": key, "result": "fail" if code else "pass",
                                 "tail": _tail(margv, out), "fingerprint": theirs})
    return bool(code) and theirs == mine


def graph_stale(ctx, lp, g):
    """True when the lane's graph file is not what build.regenerate would write: a kit lane is checked by its own
    bin/ade (CI renders with the lane's code), any other lane against the running ade's render."""
    out = lp / ctx.cfg["graph"]["out"]
    if not out.is_file():
        return True
    if (lp / "ade" / "cli.py").is_file() and (lp / graph.ROUTE).is_file():
        r = subprocess.run([sys.executable, str(lp / "bin" / "ade"), "graph", "--check"], cwd=lp, capture_output=True)
        return r.returncode != 0
    return host.read(out) != graph.render(g)


def changed_test_keys(ctx, lp):
    """The keys under [tests] in ade.toml that the lane's diff against trunk adds, drops or changes."""
    def table(text):
        try:
            return tomllib.loads(text).get("tests", {})
        except tomllib.TOMLDecodeError:
            return {}

    base = git(lp, "merge-base", main(ctx), "HEAD")
    old = git(lp, "show", f"{base}:ade.toml", check=False)
    was = table(old.stdout if old.returncode == 0 else "")
    now = table(host.read(lp / "ade.toml") if (lp / "ade.toml").is_file() else "")
    return sorted(k for k in was.keys() | now.keys() if was.get(k) != now.get(k))


def run(ctx, c):
    lp = lane_path(ctx, c["id"])
    keys = changed_test_keys(ctx, lp)
    if keys:
        # A lane that edits its own test command changes what "tests passed" means for itself: a person decides.
        refuse("tests.protected", f"the lane changes [tests] in ade.toml: {', '.join(keys)}",
               f"to land it: a person approves, then `git -C {lp} push {push.remote(ctx) or ctx.cfg['git']['remote']} HEAD:refs/heads/ade/{c['id']}`, open the PR, and "
               f"`ade card close {c['id']} --why \"approved by <name>, landed by PR\"`; otherwise revert it in {lp}, then `ade loop {c['id']}`")
    t = tree(lp)
    if cached(ctx, "test", c, t, "pass"):
        return Result("advance", "tests cached for this tree")
    g, graph_on = graph.build(lp, ctx.cfg), "graph" not in parts.off(ctx.cfg)
    if graph_on and graph_stale(ctx, lp, g):
        # The driver owns the graph: render it in the lane and commit it; the tree changed, so the cache above no longer applies.
        try:
            build.regenerate(ctx, lp)
        except (OSError, subprocess.SubprocessError) as e:
            return Result("needs-judgement", f"{ctx.cfg['graph']['out']} is stale in the lane and regenerating it failed: {str(e)[:80]}",
                          f"cd {lp} && ade graph, fix what it names, commit; then `ade loop {c['id']}`")
        git(lp, "add", "--", ctx.cfg["graph"]["out"])
        if git(lp, "diff", "--cached", "--quiet", check=False).returncode:
            git(lp, "commit", "-q", "-m", f"{c['id']}: regenerate graph")
        t, g = tree(lp), graph.build(lp, ctx.cfg)
    # With the graph part off, nothing checks or selects from the graph file: the whole suite runs.
    paths = touched(ctx, c)
    chosen = graph.select_tests(g, paths, ctx.cfg) if graph_on else None
    units, mapnote = unit_map(ctx, lp, paths)
    if units and chosen is not None:
        chosen = sorted(set(chosen) | set(units))  # the project's own runner adds units the graph cannot reach
    if mapnote:
        print(mapnote)
    if chosen == []:
        records.append(ctx, "test", {"card": c["id"], "tree": t, "tests": [], "result": "pass", "tail": ""})
        return Result("advance", "no tests reach a prose-only change")
    label = "whole suite" if chosen is None else ", ".join(chosen)
    if ctx.cfg["tests"]["always"]:
        label += f" (after {len(ctx.cfg['tests']['always'])} always-run)"
    timing = re.compile(ctx.cfg["tests"]["timing"], re.I)
    total = "whole suite" if chosen is None else f"{len(chosen)} selected"
    failed, names, carried, retried, tail, walls, results, nothing = None, "", [], [], "", {}, {}, []
    vals = values(ctx, lp)
    for template in commands(ctx.cfg, g, chosen):
        argv = host.substitute(template, **vals)
        began = time.monotonic()
        code, out = _run(ctx, argv, lp)
        if code and code != 124 and timing.search(out):  # 124 is the tests timeout: a hang costs one, not two
            retried.append(shlex.join(argv))  # one isolated retry for a timing-shaped failure, never more
            code, out = _run(ctx, argv, lp)
        if code == 5 and EMPTY.search(out):  # the runner found no tests: nothing to run, not a red
            nothing.append(shlex.join(template))
            code = 0
        # Each command's own time and result, so a typecheck's share of the stage (and which command failed) shows.
        walls[shlex.join(template)] = round(time.monotonic() - began, 2)
        results[shlex.join(template)] = "fail" if code else "pass"
        tail = _tail(argv, out)
        if code and code != 127 and red_on_main(ctx, argv, lp, out, template):
            carried.append({"test": shlex.join(template), "tail": tail[-1000:]})  # main is red too: carry, don't block
        elif code:
            # The whole failure goes to the store; the record and the screen get its head, tail and handle.
            names = failed_names(argv, out)
            note_reds(ctx, c, argv, out, g, paths)
            failed = q.emit(ctx, f"$ {shlex.join(argv)}\n{out}", cmd=shlex.join(argv), rc=code, card=c["id"])
            break
    empty = [] if failed else empty_files(ctx, lp, g, vals)
    records.append(ctx, "test", {"card": c["id"], "tree": t, "tests": chosen, "result": "fail" if failed or empty else "pass",
                                 "tail": failed or tail, "retried": retried, "carried": carried, "wall_s": walls,
                                 "results": results, "empty": empty})
    if empty:
        return Result("needs-judgement", f"{', '.join(empty)} ran no tests (the runner found none: check the test style matches [tests] command)",
                      f"rewrite the tests in {lp} for the project's runner, commit, then `ade loop {c['id']}`")
    if failed:
        return Result("needs-judgement", f"failed: {names} (of {total})",
                      f"see `ade card show {c['id']}`, fix in {lp}, commit, then `ade loop {c['id']}`")
    if carried and len(carried) == len(results):  # every command that ran was red on main too: nothing ran green
        return Result("advance", f"nothing ran green: {label}; {len(carried)} red on main too, carried to the residuals card "
                                 "(check the [tests] command with `ade config tests`)")
    if nothing and len(nothing) == len(results):
        return Result("advance", f"no tests found by {', '.join(nothing)}")
    extra = f"; {len(carried)} red on main too, carried to the residuals card" if carried else ""
    return Result("advance", f"passed: {label}{extra}")
