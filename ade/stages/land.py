# id: ade.stages.land
# what: land stage: (a land conflict or backstop stop merges trunk into the lane itself, naming only the files still unmerged, never the graph out file, which it renders on the merged tree and stages), (catch_up merges with --autostash: an uncommitted edit rides across, a conflicting one stays in the stash with the cure printed; a red suite whose reset the edit blocks says trunk is still on the merge, the files and the cure), (--catch-up: stops on a card lower in a stack, naming the top branch to catch up instead; else a landed card's (whatever [git] push says) remote ade/<card> gets fetched trunk merged in, in a scratch worktree, suite run, pushed as a fast-forward, a landed row with caught_up_from appended), (a card already landed under push = "pr" whose ade/<card> is missing on a reachable remote and whose commit is not in the fetched remote trunk is shipped again), clear land-* scratch worktrees over an hour old and prune git's worktree list (inside land.lock), stop a lane that changed a [land] ask path (or held by loop --hold-land, its question naming the card's paths) until a person answers land (a 'rework: TEXT' answer sends it back to build, then asks again), stop a lane that changed a [protected] path unless it is exactly what ade init renders (a lane that changes ade/install.py renders with its own copy, and then waits for a person's land, never an agent's), check what [graph] require asks for (noting the rest) and that the route still binds, merge one card at a time (main moved by another commit during the merge: no reset, no push, the arrivals named), note files outside the card's paths per [land] strays (each tagged deletes, adds or changes, deletions first), rerun reached tests if main moved under the lane, verdict and outcome, carry main's reds, net lines per component, then push (never a rehearsal's landing); under push = "pr" also read earlier ade/<card> PRs with gh, read-only, and carry an open one with a failed check or CHANGES_REQUESTED as a residuals row
# cmd: -
# reads: .ade/records.jsonl, lane worktree
# writes: .ade/records.jsonl (a no-default land question, verdict, outcome (with each card proof's cmd, rc and output sha256; fail_proof when [tests] fail_proof is on; new literal pins per test file; adjusted: tests changed twice without their code; outside when the lane changes files outside the card), test_adjusted (card, test), card, residual), main branch (merge), the remote when [git] push = "land" (or "pr": records, ade/<card> and a pull request), moves .ade/lanes/<card>.log to .ade/logs/ (newest 20 kept)
import ast
import os
import contextlib
import dataclasses
import hashlib
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ade import budget, graph, host, install, parts, push, records
from ade.config import KIT, load_at, matches
from ade.gitx import git
from ade.refuse import Refusal, refuse
from ade.stages import lane, review, test
from ade.stages import Result, branch, cached, lane_path, main, touched, tree

RESIDUALS = "residuals"
KEEP_LOGS = 20  # landed lanes' provider output, newest first; older ones go, their usage rows stay


def keep_log(ctx, card_id):
    # The lane log is what the engines said; kept after land under .ade/logs/ for reading, within a fixed count.
    src, out = ctx.state / "lanes" / f"{card_id}.log", ctx.state / "logs"
    if src.is_file():
        out.mkdir(exist_ok=True)
        src.replace(out / f"{card_id}.log")
    for old in sorted(out.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_LOGS:]:
        old.unlink()


def unfed(ctx, lp, c, paths, key="require"):
    """Code files the lane added or changed that lack what [graph] <key> (require or note) asks for, as
    (path, missing) pairs."""
    g, gcfg = graph.load(lp, ctx.cfg) or {"files": {}, "tests": {}}, ctx.cfg["graph"]
    want = gcfg.get(key, [])
    out = []
    for p in paths:
        f = g["files"].get(p)
        if not f or f["test"] or matches(p, ctx.cfg["rigour"]["no_review"]) or matches(p, gcfg["untested_ok"]):
            continue  # gone, a test itself, prose, or an argued exception
        missing = []
        if "tests" in want and not g["tests"].get(p):
            missing.append("a test that reaches it")
        if "header" in want and not f["header"]:
            missing.append("the five-line header")
        if missing:
            out.append((p, missing))
    return out


def unbound(lp):
    """What `ade conduct --check` says about the lane: its route and scripts against its verbs. A lane of the kit
    itself is checked by its own code, so a verb renamed in the lane is read as renamed (ARCHITECTURE.md 12.6)."""
    kit = lp if (lp / "ade" / "cli.py").is_file() and (lp / graph.ROUTE).is_file() else KIT
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(kit), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
    r = subprocess.run([sys.executable, "-m", "ade", "conduct", "--check"], cwd=lp, capture_output=True, text=True, env=env)
    return (r.stdout + r.stderr).strip() if r.returncode else ""


def kinds(ctx, c, out):
    """The strays as 'deletes X' / 'adds X' / 'changes X' from the lane diff's name-status, deletions first."""
    verb = {"D": "deletes", "A": "adds"}
    st = {}
    for line in git(lane_path(ctx, c["id"]), "diff", "--name-status", "--no-renames", f"{main(ctx)}...HEAD").splitlines():
        k, _, name = line.partition("\t")
        st[name] = verb.get(k[:1], "changes")
    rank = {"deletes": 0, "adds": 1, "changes": 2}
    return sorted((f"{st.get(p, 'changes')} {p}" for p in out), key=lambda t: rank[t.split()[0]])


def protected_changes(ctx, c, lp):
    """Paths in main...ade/<card> that match [protected] and are not exactly what `ade init` renders from the lane's
    own config (install.plan); ade.toml is never generated, so any change to it is listed. The render reads main's
    files, not the lane's: init keeps what it finds (extra allow rules, text outside AGENTS.md's markers), so a
    lane file rendered from itself would always pass."""
    names = git(ctx.root, "diff", "--name-only", "--no-renames", f"{main(ctx)}...{branch(c['id'])}").splitlines()
    return unrendered(ctx, lp, [p for p in names if matches(p, ctx.cfg["protected"]["paths"])], c)


GENERATOR = "ade/install.py"


def own_generator(ctx, c, lp):
    """The lane's own install module when the lane changes ade/install.py (the generator), else None. It is loaded
    from the lane under a unique name; sys.modules['ade.install'] stays the running kit's."""
    src = lp / GENERATOR
    if not src.is_file() or GENERATOR not in git(ctx.root, "diff", "--name-only", "--no-renames", f"{main(ctx)}...{branch(c['id'])}").splitlines():
        return None
    spec = importlib.util.spec_from_file_location(f"_lane_install_{c['id']}", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def unrendered(ctx, lp, hit, c=None):
    """The protected paths in hit whose lane file is not exactly what install.plan renders (see protected_changes);
    a lane that changes ade/install.py is rendered by its own install module (asked then waits for a person)."""
    if not hit:
        return []
    gen = own_generator(ctx, c, lp) if c else None
    want = dict((gen or install).plan(dataclasses.replace(load_at(lp), root=ctx.root)))
    return [p for p in hit if p == "ade.toml" or p not in want or not (lp / p).is_file() or host.read(lp / p) != want[p]]


REWORK = "ade decide --answer {id} land (or \"rework: TEXT\" to send the lane back to the builder with TEXT)"


def rework_note(d):
    """The TEXT of a land question answered 'rework: TEXT' (build rebuilds the lane with it), else None."""
    head, sep, text = d.get("answer", "").partition(":")
    return text.strip() if d.get("rec") == "land" and sep and head.strip().lower() == "rework" else None


def asked(ctx, c):
    """A wait for a person's 'land' when the lane changed a path [land] ask lists and no decision on the card was
    answered 'land'; the question has no default, so silence never lands it. None when nothing is asked. A lane whose protected files passed by its own ade/install.py
    asks too, naming them and the generator, and only a person's 'land' clears it, whatever [conduct] answerer says. A held
    question answered 'rework: TEXT' holds the rebuilt lane again, so land asks again; so does a newest land question
    answered anything but 'land' or 'rework: TEXT', whatever [land] hold says."""
    names = git(ctx.root, "diff", "--name-only", "--no-renames", f"{main(ctx)}...{branch(c['id'])}").splitlines()
    hit = [p for p in names if matches(p, ctx.cfg["land"]["ask"])]
    mine = [d for d in records.merged(ctx, "decision").values() if d.get("card") == c["id"]]
    q = next((d for d in mine if d.get("rec") == "land" and "answer" not in d), None)
    held, last = False, next((d for d in reversed(mine) if d.get("rec") == "land"), {})
    rehold = any(d.get("held") and rework_note(d) is not None for d in mine)
    # The newest land question answered anything but 'land' or 'rework: TEXT' (a bare 'rework') is no answer: ask again.
    rehold = rehold or ("answer" in last and not last["answer"].lower().startswith("land") and rework_note(last) is None)
    if not hit and (ctx.cfg["land"].get("hold") == c["id"] or q or rehold):  # --hold-land, its open question, or a rework
        held = q.get("held", False) if q else bool(last.get("held")) or ctx.cfg["land"].get("hold") == c["id"]  # a hold question names the card's paths, so decide --list finds it
        hit = (q or last).get("paths") or c.get("paths") or ["(--hold-land)"]
    agent_ok = ctx.cfg.get("conduct", {}).get("answerer") == "agent"  # an agent's own 'land' answer counts only then
    lp = lane_path(ctx, c["id"])
    own = [p for p in names if matches(p, ctx.cfg["protected"]["paths"]) and p != "ade.toml"] if (lp / ".git").exists() and own_generator(ctx, c, lp) else []
    if own:  # rendered by the lane's own generator: a person's answer only, never an agent's
        hit, agent_ok = hit + [p for p in own + [GENERATOR] if p not in hit], False
    if not hit or any(d.get("answer", "").lower().startswith("land") and (agent_ok or d.get("by") != "agent") for d in mine):
        return None
    why = (f"{c['id']} held by --hold-land: lands only on an answer to its question" if held
           else f"{c['id']} touches {hit[0]}, which lands only on an answer to its question")
    if q is None:
        q = records.append(ctx, "decision", {"text": why, "paths": list(hit), "kind": "question", "rec": "land",
                                             "default": "", "deadline": records.today().isoformat(), "card": c["id"],
                                             **({"held": True} if held else {}),
                                             **({"answerer": "agent"} if ctx.cfg.get("conduct", {}).get("answerer") == "agent" else {})}, prefix="d")
    return Result("wait", why if held else f"{why} ([land] ask)", REWORK.format(id=q["id"]))


def carry(ctx, c, t, sha):
    # Reds that are red on main too were let through by the test stage; they land on the one residuals card.
    row = next((r for r in reversed(records.read(ctx, "test")) if r["card"] == c["id"] and r["tree"] == t), {})
    carried = row.get("carried", [])
    residual(ctx, c["id"], carried, sha)
    return len(carried)


def residual(ctx, cid, reds, sha):
    for red in reds:
        records.append(ctx, "residual", {"card": cid, "test": red["test"], "tail": red["tail"], "seen_on_main": sha})
    if reds:
        have = records.merged(ctx, "card").get(RESIDUALS, {})
        tests = sorted(set(have.get("tests", [])) | {r["test"] for r in reds})
        records.append(ctx, "card", {"id": RESIDUALS, "title": "fix the reds carried from main and the flagged ade PRs",
                                     "paths": ["*"], "done": "each carried command passes on main, and each flagged PR is fixed or closed", "tests": tests, "status": "open"})


def follow_prs(ctx, cid):
    """Read-only: an earlier open ade/<card> PR with a failed check or CHANGES_REQUESTED becomes a residuals row.
    Never pushes, comments or merges; no gh, or a gh that fails, changes nothing."""
    gh = shutil.which("gh")
    ls = git(ctx.root, "ls-remote", "--heads", ctx.cfg["git"]["remote"], "refs/heads/ade/*", check=False, env=push.QUIET)
    seen, reds = {r["test"] for r in records.read(ctx, "residual")}, []
    for ref in (ls.stdout.split() if gh and ls.returncode == 0 else []):
        head = ref[len("refs/heads/"):]
        if not ref.startswith("refs/heads/ade/") or head == f"ade/{cid}" or f"gh pr view {head}" in seen:
            continue
        try:
            r = subprocess.run([gh, "pr", "view", head, "--json", "state,statusCheckRollup,reviewDecision"],
                               cwd=ctx.root, capture_output=True, text=True, timeout=30)
            d = json.loads(r.stdout) if not r.returncode else {}
            failed = any(x.get("conclusion") in ("FAILURE", "TIMED_OUT") for x in d.get("statusCheckRollup") or [])
        except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
            continue
        why = "a check failed" if failed else "changes were requested" if d.get("reviewDecision") == "CHANGES_REQUESTED" else ""
        if d.get("state") == "OPEN" and why:
            reds.append({"test": f"gh pr view {head}", "tail": f"{head} is open and {why}; next: `gh pr view {head} --web`"})
    residual(ctx, "pr", reds, git(ctx.root, "rev-parse", "HEAD"))


def pins(src):
    # Literal pins in a test's source: an assertEqual/assertIn/assert == against an int of 100+ or a list/tuple of 3+ items. None if it will not parse.
    def big(n):
        return (isinstance(n, ast.Constant) and type(n.value) is int and n.value >= 100) or (isinstance(n, (ast.List, ast.Tuple)) and len(n.elts) >= 3)
    try:
        nodes = list(ast.walk(ast.parse(src)))
    except (SyntaxError, ValueError):
        return None
    n = 0
    for x in nodes:
        if isinstance(x, ast.Call) and getattr(x.func, "attr", "") in ("assertEqual", "assertIn"):
            n += any(big(a) for a in x.args[:2])
        elif isinstance(x, ast.Assert) and isinstance(x.test, ast.Compare) and isinstance(x.test.ops[0], ast.Eq):
            n += any(big(a) for a in (x.test.left, *x.test.comparators))
    return n


def changed_tests(paths):
    for f in paths:
        names = Path(f).parts
        if f.endswith(".py") and (names[-1].startswith("test_") or names[-1].endswith("_test.py") or "tests" in names[:-1]):
            yield f


def adjusted(ctx, c, lp, paths):
    # Changed Python tests (present on main) whose graph imports hold none of the lane's changed non-test files: a test moved without its code.
    # Returns (all such tests, those an earlier card already adjusted).
    tests = [f for f in changed_tests(paths) if not git(ctx.root, "show", f"{main(ctx)}:{f}", check=False).returncode]
    if not tests:
        return [], []
    g = graph.build(lp, ctx.cfg)["files"]
    code = {f for f in paths if f not in tests and not g.get(f, {}).get("test")}
    out = [f for f in tests if f in g and not code & set(g[f]["imports"])]
    before = {r["test"] for r in records.read(ctx, "test_adjusted") if r["card"] != c["id"]}
    return out, [f for f in out if f in before]


def added_verbs(ctx, c):
    """Verbs (module names, dashed) the lane adds under ade/verbs."""
    out = git(ctx.root, "diff", "--name-only", "--diff-filter=A", f"{main(ctx)}...{branch(c['id'])}", "--", "ade/verbs")
    return [Path(p).stem.replace("_", "-") for p in out.splitlines() if p.endswith(".py") and not Path(p).name.startswith("_")]


def new_pins(ctx, c, paths):
    # Per changed test file, how many literal pins the lane adds over main; only files whose count rose.
    out = {}
    for f in changed_tests(paths):
        new = git(ctx.root, "show", f"{branch(c['id'])}:{f}", check=False)
        if new.returncode:
            continue
        old = git(ctx.root, "show", f"{main(ctx)}:{f}", check=False)
        a, b = pins(new.stdout), (pins(old.stdout) if not old.returncode else 0)
        if a is not None and b is not None and a > b:
            out[f] = a - b
    return out


@contextlib.contextmanager
def merged_tree(ctx, rev="HEAD"):
    # rev (default HEAD) checked out detached beside the main checkout, so the person's uncommitted edits never colour a test result.
    ctx.state.mkdir(parents=True, exist_ok=True)
    wt = tempfile.mkdtemp(prefix="land-", dir=ctx.state)
    try:
        git(ctx.root, "worktree", "add", "-q", "--detach", wt, rev)
        lane.share(ctx, Path(wt))  # the [lanes] share links (node_modules, .venv), as every lane gets them
        yield Path(wt)
    finally:
        lane.unshare(ctx, Path(wt))
        git(ctx.root, "worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)


def clear_stale(ctx, age=3600):
    # Scratch land-* worktrees an earlier landing left behind (cut off mid-run); a younger one may be a catch-up's, so it stays.
    for d in ctx.state.glob("land-*"):
        try:
            old = time.time() - d.stat().st_mtime > age
        except OSError:
            continue
        if old and d.is_dir():
            git(ctx.root, "worktree", "remove", "--force", str(d), check=False)
            shutil.rmtree(d, ignore_errors=True)
    git(ctx.root, "worktree", "prune", check=False)


def dirty(ctx):
    # Paths the person has changed in the main checkout, staged or not, plus untracked files.
    return set(git(ctx.root, "diff", "--name-only", "--no-renames", "HEAD").splitlines()) | \
        set(git(ctx.root, "ls-files", "--others", "--exclude-standard").splitlines())


def backstop(ctx, c, t, before, rev="HEAD"):
    """After the merge, the tests the change reaches, run on main itself; the first command that fails, or None.
    Only when main moved under the lane: otherwise the merged tree is the tree the lane already tested. A command
    the lane itself failed was red on main too and carried, so it is not this card's clash."""
    if git(ctx.root, "merge-base", before, branch(c["id"])) == before:
        return None
    with merged_tree(ctx, rev) as wt:
        g = graph.build(wt, ctx.cfg)
        chosen = graph.select_tests(g, git(ctx.root, "diff", "--name-only", before, rev).splitlines(), ctx.cfg)
        if chosen == []:
            return None
        lane_row = next((r for r in reversed(records.read(ctx, "test")) if r["card"] == c["id"] and r["tree"] == t), {})
        vals = test.values(ctx, wt, before)
        for template in test.commands(ctx.cfg, g, chosen):
            code, _ = test._run(ctx, host.substitute(template, **vals), wt)
            if code and lane_row.get("results", {}).get(shlex.join(template)) != "fail":
                return shlex.join(template)
    return None


def lane_merge(ctx, c, lp, check=None):
    """Merge trunk into the lane here (a merge commit, never a rebase), so paths a sandboxed seat cannot write
    (.claude/skills, agents, settings) arrive through ADE's own git. Returns the cure: the unmerged files, or clean;
    check (the command failing on main, from the backstop) adds fixing it in the lane before the loop."""
    r = git(lp, "merge", "--no-ff", "-q", "--autostash", "-m", f"catch up {c['id']} with {main(ctx)}", main(ctx), check=False)
    kept = " (your uncommitted edit conflicts with trunk and is kept in the stash: `git stash list`, then pop and resolve)" if not r.returncode and "resulted in conflicts" in (r.stderr or "") + (r.stdout or "") else ""
    if not r.returncode:
        fix = f"fix `{check}` in the lane, commit, then " if check else ""
        return f"trunk is merged into the lane with no clash{kept}; {fix}`ade loop {c['id']}`"
    files = git(lp, "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
    out = ctx.cfg["graph"]["out"]
    if out in files:  # the generated graph is never a file to resolve: render it on the merged tree and stage it
        from ade.stages import build  # build imports land, so only here
        try:
            build.regenerate(ctx, lp)
            git(lp, "add", "--", out)
            files.remove(out)
        except subprocess.CalledProcessError:
            pass
        if not files and not git(lp, "commit", "-q", "--no-edit", check=False).returncode:
            fix = f"fix `{check}` in the lane, commit, then " if check else ""
            return f"trunk is merged into the lane with the graph rendered anew; {fix}`ade loop {c['id']}`"
    if not files:
        git(lp, "merge", "--abort", check=False)
        return f"the lane could not take trunk ({(r.stderr or r.stdout).strip()[:200]}); in {lp} run `git merge {main(ctx)}`, then `ade loop {c['id']}`"
    return f"trunk is merged into the lane except {', '.join(files)} (markers left in {lp}); {'resolve those files, ' + f'fix `{check}`, commit' if check else 'edit those files in the lane, git commit'}, then `ade loop {c['id']}`"


def regenerated(ctx, c):
    """A merge stopped on conflicts: if the graph out file is the only unmerged path, render it on the merged tree,
    stage it and commit the merge; True when done. Any other conflicting path leaves the merge for the caller to abort."""
    from ade.stages import build  # build imports land, so only here
    out = ctx.cfg["graph"]["out"]
    if git(ctx.root, "diff", "--name-only", "--diff-filter=U").splitlines() != [out]:
        return False
    try:
        build.regenerate(ctx, ctx.root)
    except subprocess.CalledProcessError:  # the render failed: leave the merge for the caller to abort
        return False
    git(ctx.root, "add", "--", out)
    return not git(ctx.root, "commit", "-q", "--no-edit", check=False).returncode


def catch_up(ctx, cid=None):
    """The remote trunk moved: merge it in (a merge commit, never a rebase or a force) and rerun the full suite on the
    result. True when trunk is ready to push again; on a conflict or a red suite trunk goes back to where it was."""
    name, trunk = ctx.cfg["git"]["remote"], main(ctx)
    before = git(ctx.root, "rev-parse", "HEAD")
    if git(ctx.root, "fetch", "-q", name, f"refs/heads/{trunk}", check=False, env=push.QUIET).returncode:
        return False
    merge = git(ctx.root, "merge", "-q", "--no-ff", "--autostash", "-m", f"catch up with {name}/{trunk}", "FETCH_HEAD", check=False)
    if not merge.returncode and "resulted in conflicts" in (merge.stderr or "") + (merge.stdout or ""):  # the edit would not go back on cleanly
        print("your uncommitted edit conflicts with the merge and is kept in the stash\n"
              "cure: git stash list; resolve, then git stash drop")
    if merge.returncode and not regenerated(ctx, None):  # a graph-only clash is rendered anew, as land's own merge does
        clash = git(ctx.root, "diff", "--name-only", "--diff-filter=U").splitlines()
        if clash:
            print("conflict in: " + ", ".join(clash))
        else:  # the merge never started (say, local changes it would overwrite): git's first error line and the files it names
            lines = (merge.stderr or merge.stdout or "").splitlines()
            files = [l.strip() for l in lines if l.startswith(("\t", " ")) and l.strip()]
            print("merge did not start: " + next((l for l in lines if l.strip()), "git gave no reason")
                  + (f"\ncure: commit or stash {' '.join(files)}, then ade land {cid or ''}".rstrip() if files else ""))
        git(ctx.root, "merge", "--abort", check=False)
        return False
    with merged_tree(ctx) as wt:
        g = graph.build(wt, ctx.cfg)
        vals = test.values(ctx, wt, before)
        outs = [test._run(ctx, host.substitute(t, **vals), wt) for t in test.commands(ctx.cfg, g, None)]
    red = [out for rc, out in outs if rc]
    if red:
        print("red after merge: " + (red[0].strip().splitlines() or [""])[-1])
        back = git(ctx.root, "reset", "-q", "--keep", before, check=False)
        if back.returncode:  # the carried edit touches a file the merge changed: trunk stays on the merge
            err = (back.stderr or "") + (back.stdout or "")
            files = sorted(set(re.findall(r"Entry '([^']+)'", err))) or ["(git named none)"]
            print(f"trunk is still on the catch-up merge: git reset --keep refused over {', '.join(files)}\n"
                  f"cure: commit or stash them, then git reset --keep {before}, then ade land {cid or ''}".rstrip())
        return False
    return True


def open_pr(ctx, c, sha=None):
    """[git] push = "pr": trunk stays put. The landing's merge commit goes up as ade/<card> (never forced) and gh
    opens the pull request; without gh, or if it fails, the cure is the command to run by hand."""
    name, trunk = ctx.cfg["git"]["remote"], main(ctx)
    # Follow the merge commits the remote makes, so this card branch carries only its own work.
    fetched = git(ctx.root, "fetch", "-q", name, f"refs/heads/{trunk}", check=False, env=push.QUIET).returncode == 0
    if fetched:
        push.prune_merged(ctx)  # FETCH_HEAD is still the fetched trunk here
    land_sha, stuck = git(ctx.root, "rev-parse", "HEAD"), False
    # A re-ship sends the card's own landed commit (sha), never the main checkout's HEAD, and catches nothing up.
    if fetched and not sha and git(ctx.root, "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD", check=False).returncode:
        stuck = not catch_up(ctx, c and c["id"])
        if not stuck:  # the newest landed row names the pushed head; written before the records push so it goes up too
            records.append(ctx, "card", {"id": c["id"], "status": "landed", "commit": git(ctx.root, "rev-parse", "HEAD"),
                                         "caught_up_from": land_sha})
    elif fetched and sha and sha != land_sha and git(ctx.root, "rev-parse", "-q", "--verify", "HEAD^2", check=False).returncode == 0 \
            and git(ctx.root, "rev-parse", "HEAD^1").strip() == sha \
            and git(ctx.root, "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD", check=False).returncode == 0:
        # a person merged the remote trunk into the landed commit by hand: record it as catch_up would, and ship that head
        records.append(ctx, "card", {"id": c["id"], "status": "landed", "commit": land_sha, "caught_up_from": sha})
        sha = None
    try:
        if push.push(ctx, trunk=False) is None:
            return ""  # no remote: nothing to push to, as with push = "land"
    except Refusal as r:
        print(r.render())
        records.fired(ctx, r, c["id"])
        return "; not pushed (see above)"
    cid = ctx.renumbered.get(c["id"], c["id"])  # the records push may have renumbered this card
    head = f"ade/{cid}"
    tip = sha or git(ctx.root, "rev-parse", "HEAD")
    row = records.merged(ctx, "card").get(cid, {})
    if row.get("status") == "landed" and row.get("commit") != tip:  # a renumbered landing is re-pointed: say so, append the true row
        print(f"{cid}'s landed row cites {(row.get('commit') or '?')[:10]} but {head} points at {tip[:10]}; a corrected row is appended")
        records.append(ctx, "card", {"id": cid, "status": "landed", "commit": tip})
        try:
            push.push(ctx, trunk=False)  # the corrected row goes up with the records branch
        except Refusal as r:
            print(r.render())
            records.fired(ctx, r, cid)
    first = c["title"].split(". ")[0].rstrip(".")
    if len(first) > 72:  # cut at a whole word, "..." included in the 72
        first = first[:69].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..." if " " in first[:70] else first[:69] + "..."
    title = f"{cid}: {first}"  # short; the full title is in the body
    body = f"{c['title']}\n\n" + (f"Why: {c['why']}\n\n" if c.get("why") else "") + f"Done when: {c.get('done', '')}"
    cmd = ["gh", "pr", "create", "--base", trunk, "--head", head, "--title", title, "--body-file", "-"]  # body on stdin: cmd.exe ends an argument at a newline
    if stuck:
        print(f"{name}/{trunk} could not be merged into {trunk}; nothing was pushed\n"
              f"cure: merge {name}/{trunk} by hand; the next `ade land {c['id']}` records it and pushes {head}")
        return "; not pushed (see above)"
    sent = git(ctx.root, "push", "-q", name, f"{tip}:refs/heads/{head}", check=False,
               env=push.QUIET)
    if sent.returncode:  # say, ade/<card> is already on the remote at another commit: never forced
        print(f"{name} refused {head}; nothing was forced ({sent.stderr.strip()})\ncure: delete or rename the remote "
              f"{head}, then `git push {name} HEAD:refs/heads/{head}`")
        return "; not pushed (see above)"
    gh = shutil.which("gh")  # the resolved path, so Windows runs gh.exe or a gh.cmd the same way which found it
    r = subprocess.run([gh, *cmd[1:]], cwd=ctx.root, capture_output=True, text=True, encoding="utf-8",
                       input=body) if gh else None
    if r and not r.returncode and r.stdout.strip():
        return f"; PR opened: {r.stdout.strip().splitlines()[-1]}"
    why = (r.stderr.strip() or "gh failed") if r else "gh is not on PATH"
    if gh:  # some hosts refuse the GraphQL that `gh pr create` uses: try REST once
        api = [gh, "api", "repos/{owner}/{repo}/pulls", "--method", "POST", "-f", f"base={trunk}", "-f",
               f"head={head}", "-f", f"title={title}", "-F", "body=@-"]
        a = subprocess.run(api, cwd=ctx.root, capture_output=True, text=True, encoding="utf-8", input=body)
        try:
            url = json.loads(a.stdout)["html_url"] if not a.returncode else ""
        except (ValueError, KeyError, TypeError):
            url = ""
        if url:
            return f"; PR opened: {url}"
    print(f"{head} is pushed but no pull request was opened ({why}).\ncure: run `{shlex.join(cmd)}`")
    return "; branch pushed, no PR (see above)"


def ship(ctx, c=None, sha=None):
    # After every record is written, so the remote gets the outcome too. The merge is already done and stays done:
    # a refused push is printed with its cure, and the next landing or `ade push` tries again.
    mode = ctx.cfg["git"]["push"]
    if mode not in ("land", "pr"):
        return ""
    # A rehearsal's landing is never real work: the fake provider wrote it, so it stays local. Every ade run acts on
    # the repo's main checkout, even from a worktree of it, so this is the one guard that keeps a probe off the remote;
    # `ade push` still pushes on purpose.
    if os.environ.get("ADE_PROVIDER") == "fake":
        return "; not pushed (a rehearsal: the fake provider built it)"
    if mode == "pr" and c:
        follow_prs(ctx, c["id"])  # before open_pr, so the earlier PRs are read and the new one is not
        return open_pr(ctx, c, sha)
    try:
        try:
            rows = push.push(ctx)
        except Refusal as r:
            if r.code != "push.rejected" or not catch_up(ctx, c and c["id"]):
                raise
            rows = push.push(ctx)
    except Refusal as r:
        print(r.render())
        records.fired(ctx, r)
        return "; not pushed (see above)"
    return "" if rows is None else f"; pushed to {ctx.cfg['git']['remote']}"


def merge_of(ctx, c):
    """The merge commit on main that took this lane's tip and its first parent, or None: a landing cut off after its merge is finished, not redone."""
    tip = git(ctx.root, "rev-parse", branch(c["id"]))
    for line in git(ctx.root, "log", "--merges", "--first-parent", "--format=%H %P", main(ctx)).splitlines():
        sha, *parents = line.split()
        if len(parents) == 2 and parents[1] == tip:
            return sha, parents[0]
    return None


def fails_without(ctx, paths, main_sha, sha):
    # [tests] fail_proof: the changed tests rerun on the merge with its changed non-test files put back as main has them.
    # True when they fail there, False when they pass, None when there is nothing to compare. A note only: never blocks.
    tests = list(changed_tests(paths))
    if all(f in tests for f in paths):
        return None  # a test-only card: no code to put back, and 'pass without the change' would read as a vacuous test
    with merged_tree(ctx, sha) as wt:
        tests = [f for f in tests if (wt / f).is_file()]
        for f in (f for f in paths if f not in tests):
            if git(ctx.root, "cat-file", "-e", f"{main_sha}:{f}", check=False).returncode:
                (wt / f).unlink(missing_ok=True)  # new in the card: absent on main
            else:
                git(wt, "checkout", main_sha, "--", f)
        vals = test.values(ctx, wt, main_sha)
        run = [host.substitute(t, **vals) for t in test.commands(ctx.cfg, graph.build(wt, ctx.cfg), tests)] if tests else []
        return any(test._run(ctx, argv, wt)[0] for argv in run) if run else None


def moved_under(ctx, c, sha, at=None):
    """A stop when something other than `at` (main's HEAD as land found it; the merge sha when fresh) is now HEAD, else None.
    Land then neither resets nor pushes, so the arrival is never rolled back or shipped by accident."""
    at = at or sha
    if git(ctx.root, "rev-parse", "HEAD") == at:
        return None
    arrived = git(ctx.root, "log", "--oneline", f"{at}..HEAD", check=False).stdout.strip() or "(main left the merge)"
    return Result("needs-judgement", f"main moved under the landing of {c['id']}; nothing was reset or pushed. Arrived:\n{arrived}",
                  f"move those commits off {main(ctx)}, or rerun after they are in, then `ade loop {c['id']}`")


def prove(ctx, c, lp, main_sha, sha, at=None):
    """Run the card's proofs on the merge at sha: (rows, None), or (rows, the stop) on the first non-zero rc.
    Main goes back to main_sha only while the merge is not on the remote; once pushed it is left and the stop says so."""
    rows = []
    for cmd in c.get("proofs", []):  # the child's own rc and output, run on the merged main; never typed in
        try:
            code, out = test._run(ctx, test.split(cmd), ctx.root)
        except (OSError, ValueError) as e:  # a proof that cannot start is a failed proof, not a crash with main merged
            code, out = 127, str(e)
        rows.append({"cmd": cmd, "rc": code, "out_sha256": hashlib.sha256(out.encode()).hexdigest()})
        if code:
            name = push.remote(ctx)
            ref = f"{name}/{main(ctx)}"
            pushed = name and git(ctx.root, "merge-base", "--is-ancestor", sha, ref, check=False).returncode == 0
            if pushed:
                return rows, Result("needs-judgement", f"the proof `{cmd}` exited {code} on main, and the merge is already pushed",
                                    f"fix forward on main with a new card; main was not reset")
            if stop := moved_under(ctx, c, sha, at):  # a commit arrived while the proof ran: never reset it away
                return rows, stop
            if at not in (None, sha):  # a resumed landing with later landings above its merge: resetting would drop them
                return rows, Result("needs-judgement", f"the proof `{cmd}` exited {code} on main, and later landings sit above the merge",
                                    f"fix forward on main with a new card; main was not reset")
            git(ctx.root, "reset", "-q", "--keep", main_sha)
            return rows, Result("needs-judgement", f"the proof `{cmd}` exited {code} on main as it would be merged",
                                f"cd {lp} && fix the change or the proof, commit; then `ade loop {c['id']}`")
    return rows, None


def unmerged(ctx, name, c):
    """True when remote trunk, freshly fetched, lacks the card's landed commit (a merged card's branch is gone by design)."""
    if git(ctx.root, "fetch", "-q", name, f"refs/heads/{main(ctx)}", check=False, env=push.QUIET).returncode:
        return False
    sha = c.get("commit")
    if not sha or git(ctx.root, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode:
        return False  # no commit on the row, or the object is not here: nothing is re-shipped on a guess
    return git(ctx.root, "merge-base", "--is-ancestor", sha, "FETCH_HEAD", check=False).returncode == 1


def stacked_above(ctx, name, head, tip):
    """The name of another fetched remote ade/* branch that contains tip ('' when none): the card is lower in a stack."""
    if git(ctx.root, "fetch", "-q", name, "+refs/heads/ade/*:refs/ade-stack/*", check=False, env=push.QUIET).returncode:
        return ""
    pre = "refs/ade-stack/"
    refs = git(ctx.root, "for-each-ref", "--format=%(refname)", pre).split()
    found = ""
    for ref in refs:
        if not found and ref != pre + head.removeprefix("ade/") and git(ctx.root, "merge-base", "--is-ancestor", tip, ref, check=False).returncode == 0:
            found = "ade/" + ref[len(pre):]
    for ref in refs:
        git(ctx.root, "update-ref", "-d", ref, check=False)
    return found


def hand_extra(ctx, old, tip, trunk):
    """What keeps tip from being a pure hand merge of trunk on the landed commit old ('' when it is one): extra commits or files."""
    ps = git(ctx.root, "rev-list", "--parents", "-n1", tip).split()[1:]
    if len(ps) != 2:
        return f"the tip has {len(ps)} parent(s), not 2"
    if ps[0] != old:
        return "extra commits: " + " ".join(git(ctx.root, "rev-list", f"{old}..{ps[0]}", check=False).stdout.split() or [ps[0]])
    if git(ctx.root, "merge-base", "--is-ancestor", ps[1], trunk, check=False).returncode:
        return f"its second parent {ps[1][:10]} is not in the fetched trunk"
    m = git(ctx.root, "merge-tree", "--write-tree", "--name-only", old, ps[1], check=False).stdout.split("\n\n")[0].splitlines()
    if not m:
        return "its merge cannot be recomputed"
    differ = git(ctx.root, "diff", "--name-only", m[0], tip).splitlines()
    extra = [f for f in differ if f not in m[1:]]
    return "extra files: " + ", ".join(extra) if extra else ""


def catch_up_branch(ctx, c):
    """ade land CARD --catch-up: merge fetched trunk into the remote ade/<card> tip in a scratch worktree (a merge commit,
    never a rebase), run the whole suite there, then push the result as a fast-forward and record it. Moves nothing on a stop."""
    name, head = push.remote(ctx), branch(c["id"])
    if c.get("status") != "landed":
        return Result("needs-judgement", f"{c['id']} is not a landed card", "nothing to catch up")
    if not name:
        return Result("needs-judgement", f"{c['id']} has no reachable remote", "nothing to catch up")
    if not unmerged(ctx, name, c):
        return Result("done", f"{c['id']} is in {name}/{main(ctx)} (or unknown here); nothing to catch up")
    trunk = git(ctx.root, "rev-parse", "FETCH_HEAD")
    if git(ctx.root, "fetch", "-q", name, f"refs/heads/{head}", check=False, env=push.QUIET).returncode:
        return Result("needs-judgement", f"{name} has no {head} to catch up", f"`ade land {c['id']}` ships it first")
    tip = git(ctx.root, "rev-parse", "FETCH_HEAD")
    if git(ctx.root, "merge-base", "--is-ancestor", trunk, tip, check=False).returncode == 0:
        old = c.get("commit")
        if old and old != tip and git(ctx.root, "merge-base", "--is-ancestor", old, tip, check=False).returncode == 0:
            # a person merged trunk in by hand: record it as the catch-up below would, but only a pure merge
            extra = hand_extra(ctx, old, tip, trunk)
            if extra:
                return Result("needs-judgement", f"{head} is not a pure merge of {name}/{main(ctx)} on {old[:10]}: {extra}; nothing was recorded",
                              f"review the extra work, or redo the merge on {old[:10]}, push it, then rerun `ade land {c['id']} --catch-up`")
            records.append(ctx, "card", {"id": c["id"], "status": "landed", "commit": tip, "caught_up_from": old})
            try:
                push.push(ctx, trunk=False)
            except Refusal as r:
                print(r.render())
                records.fired(ctx, r, c["id"])
            return Result("done", f"{head} already holds {name}/{main(ctx)}; recorded the hand merge: {old[:10]} -> {tip[:10]}")
        return Result("done", f"{head} already holds {name}/{main(ctx)}; nothing to catch up")
    above = stacked_above(ctx, name, head, tip)
    if above:  # a catch-up merge on a lower branch takes it out of the stack, so its PR stops closing with the top one
        return Result("needs-judgement", f"{name}/{above} already contains {head}'s head; catching {head} up would unstack it",
                      f"`ade land {above.removeprefix('ade/')} --catch-up` (catch up the top of the stack)")
    cure = f"merge {name}/{main(ctx)} into {head} by hand, push it, then rerun `ade land {c['id']} --catch-up` to record it"
    with merged_tree(ctx, tip) as wt:
        if git(wt, "merge", "--no-ff", "-q", "-m", f"catch up with {name}/{main(ctx)}", trunk, check=False).returncode:
            return Result("needs-judgement", f"{head} conflicts with {name}/{main(ctx)}; nothing was pushed", cure)
        g = graph.build(wt, ctx.cfg)
        vals = test.values(ctx, wt, trunk)
        if any(test._run(ctx, host.substitute(t, **vals), wt)[0] for t in test.commands(ctx.cfg, g, None)):
            return Result("needs-judgement", f"the suite is red on {head} merged with {name}/{main(ctx)}; nothing was pushed", cure)
        new = git(wt, "rev-parse", "HEAD")
    sent = git(ctx.root, "push", "-q", name, f"{new}:refs/heads/{head}", check=False, env=push.QUIET)
    if sent.returncode:
        return Result("needs-judgement", f"{name} refused {head}; nothing was forced ({sent.stderr.strip()})", cure)
    records.append(ctx, "card", {"id": c["id"], "status": "landed", "commit": new, "caught_up_from": tip})
    try:
        push.push(ctx, trunk=False)
    except Refusal as r:
        print(r.render())
        records.fired(ctx, r, c["id"])
    return Result("done", f"{head} caught up: {tip[:10]} -> {new[:10]}")


def run(ctx, c, catch=False):
    if catch:
        return catch_up_branch(ctx, c)
    if c.get("status") == "landed":
        done = f"already landed at {(c.get('commit') or '?')[:10]}"  # a landed row may carry commit: null
        name = push.remote(ctx) if ctx.cfg["git"]["push"] == "pr" else None
        if name:  # a landing cut off before its push: the branch is missing on a reachable remote, so ship it now
            r = git(ctx.root, "ls-remote", "--heads", name, f"refs/heads/{branch(c['id'])}", check=False, env=push.QUIET)
            if r.returncode == 0 and not r.stdout.strip() and unmerged(ctx, name, c):
                return Result("done", f"{done}; its branch was never pushed{ship(ctx, c, c['commit'])}")
        return Result("done", done)
    lp = lane_path(ctx, c["id"])
    t = tree(lp) if (lp / ".git").exists() else None
    if not t or not (cached(ctx, "review", c, t, "approve") and cached(ctx, "test", c, t, "pass")):
        refuse("land.unproven", f"{c['id']} has no approved review and passing tests for its current lane tree",
               f"run `ade loop {c['id']}`, which reviews and tests before it lands")
    on = git(lp, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    if on != branch(c["id"]):  # the proof is of this HEAD's tree, but the merge takes the branch: they must be one
        refuse("land.unproven", f"the lane {lp} is on {on if on != 'HEAD' else 'a detached HEAD'}, not {branch(c['id'])}, so its proven tree is not what would merge",
               f"in {lp} run `git switch {branch(c['id'])}` (bring any commits made elsewhere onto it first), then `ade loop {c['id']}`")
    loose = git(lp, "diff", "--name-only", "HEAD").splitlines() + git(lp, "ls-files", "--others", "--exclude-standard").splitlines()
    if loose:  # the tests ran on these, but only commits merge, and the lane is removed with --force after landing
        return Result("needs-judgement", f"the lane has uncommitted edits that landing would discard: {', '.join(loose)}",
                      f"commit them in {lp}, or have the project ignore files its tests write, then `ade loop {c['id']}`")
    prot = protected_changes(ctx, c, lp)
    if prot:  # whatever wrote it: the hooks only see Claude's own tools
        refuse("guard.protected", f"the lane changes protected config {', '.join(prot)}",
               f"a person edits {prot[0]} on trunk and commits it (`git commit {prot[0]} -m \"config: ...\"`), then reruns `ade init`; "
               f"or revert it in {lp}, then `ade loop {c['id']}`")
    ask = asked(ctx, c)
    if ask:
        return ask
    paths = touched(ctx, c)  # once: every check below reads the lane diff from this list
    out = review.strays(ctx, c, paths)
    mode = ctx.cfg["land"]["strays"]
    noted_kinds = kinds(ctx, c, out) if out else []  # read while the lane still differs from main
    hungry = [] if "graph" in parts.off(ctx.cfg) else unfed(ctx, lp, c, paths)  # graph part off: nothing to feed
    if hungry:
        need = "; ".join(f"{p} needs {' and '.join(m)}" for p, m in hungry)
        return Result("needs-judgement", f"the change does not feed the graph: {need}",
                      f"in {lp}, add what is missing, run `ade graph`, commit, then `ade loop {c['id']}`")
    drift = unbound(lp)
    if drift:
        refuse("route.unbound", f"the route or a script no longer matches the verbs it runs:\n{drift}",
               f"in {lp}, change each line named to what the verb now takes (or route the new stop), run `ade graph`, "
               f"commit, then `ade loop {c['id']}`")
    # What [graph] note lists lands anyway (read before the merge, while the lane still differs from main); the outcome keeps it, so the gap is visible without blocking work.
    noted = {} if "graph" in parts.off(ctx.cfg) else {p: m for p, m in unfed(ctx, lp, c, paths, "note")}
    # A verb added with no scenario flying it: a note only, as with untested code.
    rh = lp / "ade" / "verbs" / "rehearse.py"
    flown = rh.read_text(encoding="utf-8") if rh.is_file() and (lp / "ade" / "cli.py").is_file() else None  # kit-only
    unrun = [] if flown is None else [v for v in added_verbs(ctx, c) if '["' + v + '"' not in flown]
    found = new_pins(ctx, c, paths)  # a note only: never blocks
    moved, twice = adjusted(ctx, c, lp, paths)  # likewise
    # One runway: lanes build side by side, but only one lands at a time, so each merge and its check see a main
    # that holds still.
    ctx.state.mkdir(parents=True, exist_ok=True)
    with host.lock(ctx.state / "land.lock"):
        clear_stale(ctx)
        head = git(ctx.root, "rev-parse", "--abbrev-ref", "HEAD")
        if head != main(ctx):
            return Result("wait", f"the main checkout is on {head}", f"git switch {main(ctx)}, then `ade loop {c['id']}`")
        # Edits the merge does not touch stay where they are; only a path both bring is a wait.
        shared = sorted(dirty(ctx) & set(git(ctx.root, "diff", "--name-only", f"HEAD...{branch(c['id'])}").splitlines()))
        if shared:
            return Result("wait", f"the main checkout has uncommitted changes to paths this card changes: {', '.join(shared)}",
                          f"commit or stash them in {ctx.root}, then `ade loop {c['id']}`")
        done = merge_of(ctx, c)
        if done:  # cut off after the merge: write what is missing for that merge, never a second one
            sha, main_sha = done
            net = budget.net_by_component(ctx, f"{main_sha}...{branch(c['id'])}")
            at = git(ctx.root, "rev-parse", "HEAD")  # main as found: later landings may sit past the old merge
        else:
            main_sha = git(ctx.root, "rev-parse", "HEAD")
            net = budget.net_by_component(ctx, f"{main(ctx)}...{branch(c['id'])}")
            r = git(ctx.root, "merge", "--no-ff", "-q", "-m", f"land {c['id']}: {c['title']}" + (f"\n\nWhy: {c['why']}" if c.get("why") else ""), branch(c["id"]), check=False)
            if r.returncode and regenerated(ctx, c):  # only the generated graph clashed: render it on the merged tree
                r = subprocess.CompletedProcess([], 0)
            if r.returncode:
                clash = git(ctx.root, "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
                git(ctx.root, "merge", "--abort", check=False)
                if not clash:  # git refused before merging anything (say, a staged file): its reason, not a conflict
                    why = next((l.strip() for l in (r.stderr or r.stdout or "").splitlines() if l.strip()), "git gave no reason")
                    return Result("wait", f"git would not merge {branch(c['id'])}: {why[:200]}",
                                  f"commit or stash in {ctx.root}, then `ade loop {c['id']}`")
                return Result("needs-judgement", "merge conflict with main", lane_merge(ctx, c, lp))
            sha = at = git(ctx.root, "rev-parse", "HEAD")
        if not done or not any(r["card"] == c["id"] and r["tree"] == t for r in records.read(ctx, "verdict")):
            # fresh, or cut off before its verdict was written: the merged tree is checked either way
            clashed = backstop(ctx, c, t, main_sha, sha)
            if clashed:
                if stop := moved_under(ctx, c, sha):  # past the merge: never reset away later landings
                    return stop
                git(ctx.root, "reset", "-q", "--keep", main_sha)  # not pushed yet, so main simply goes back
                return Result("needs-judgement", f"main moved since this lane was tested, and on main as it is now "
                              f"`{clashed}` fails", lane_merge(ctx, c, lp, clashed))
            hard, soft = budget.over(budget.measure(ctx))
            over = [f"{n} {v} / {lim}" for n, v, lim in hard]
            if over:
                if stop := moved_under(ctx, c, sha):  # past the merge: never reset away later landings
                    return stop
                git(ctx.root, "reset", "-q", "--keep", main_sha)  # not pushed yet, so main simply goes back
                return Result("needs-judgement", f"the merge leaves the budget over: {'; '.join(over)}; nothing was pushed",
                              f"cd {lp}, trim that many lines from the named component (or retire code it no longer needs), "
                              f"commit; then `ade loop {c['id']}`")
            if soft:
                print("over its component budget (note): " + "; ".join(f"{n} {v} / {lim}" for n, v, lim in soft))
            if stop := moved_under(ctx, c, sha, at):  # before the proofs (which may reset) and the push
                return stop
        proofs, stop = prove(ctx, c, lp, main_sha, sha, at)
        if stop:
            return stop
        flip = fails_without(ctx, paths, main_sha, sha) if ctx.cfg["tests"].get("fail_proof") else None
        if stop := moved_under(ctx, c, sha, at):  # a hand commit made while proofs ran is neither recorded nor pushed
            return stop
        rows = budget.measure(ctx)
        seen = {r["test"] for r in records.read(ctx, "test_adjusted") if r["card"] == c["id"]}
        for f in moved:
            if f not in seen:
                records.append(ctx, "test_adjusted", {"card": c["id"], "test": f})
        records.append(ctx, "verdict", {"card": c["id"], "tree": t, "result": "land"})
        records.append(ctx, "outcome", {"card": c["id"], "commit": sha, "net": net,
                                        "budget": {n: v for n, v, _ in rows}, **({"noted": noted} if noted else {}),
                                        **({"proofs": proofs} if proofs else {}), **({"pins": found} if found else {}),
                                        **({} if flip is None else {"fail_proof": flip}),
                                        **({"adjusted": twice} if twice else {}),
                                        **({"outside": out} if out else {})})
        records.append(ctx, "card", {"id": c["id"], "status": "landed", "commit": sha})
        reds = carry(ctx, c, t, main_sha)
        lane.unshare(ctx, lp)
        # Forced: the lane's commits are merged, and what is left in it is what its tests wrote (a log, a cache).
        git(ctx.root, "worktree", "remove", "--force", str(lp))
        git(ctx.root, "branch", "-q", "-d", branch(c["id"]))
        keep_log(ctx, c["id"])
        summary = " ".join(f"{k} {v:+d}" for k, v in net.items()) or "no line changes"
        if not budget.off(ctx):
            print(f"merge summary: net lines by component: {summary}\n{budget.render(rows)}")
        if noted:
            print("noted, not blocking: " + "; ".join(f"{p} lacks {' and '.join(m)}" for p, m in noted.items()))
        if out and mode != "off":
            print(f"landed with files outside the card: {', '.join(noted_kinds)}")
        if flip is False:
            print("noted, not blocking: tests pass without the change")
        for v in unrun:
            print(f"noted, not blocking: verb {v} is flown by no rehearse scenario (`ade rehearse --coverage`)")
        for f, n in found.items():
            print(f"noted, not blocking: {n} new literal pins in {f}")
        for f in twice:
            print(f"noted, not blocking: {f} was adjusted without its code twice; derive its expectation or delete it")
        if reds:
            print(f"carried {reds} red(s) that are red on main too: see `ade card show {RESIDUALS}`")
        return Result("done", f"landed {sha[:10]}{ship(ctx, c)}")
