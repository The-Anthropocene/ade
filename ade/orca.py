# id: ade.orca
# what: the Orca adapter: show lanes as Orca worktrees with a terminal following each card, and the board; never drives
# cmd: -
# reads: ade.toml [cockpit], $ORCA_CLI_COMMAND / $ORCA_DEV_REPO_ROOT / $ORCA_RUNTIME_CLIENT, orca's JSON replies
# writes: Orca's view only (repo, its imported lanes, worktree status and comment, terminals); .ade/cockpit.command (macOS launcher), .ade/cockpit.pid (launch claim); nothing else in the repo
import json
import os
import shutil
import subprocess
import sys
import time

from ade import install
from ade.display import Unavailable

# Orca is the glass cockpit display: everything here is best-effort, and a failure is a printed note, never a stop.
# The calls are settled against Orca 1.4.232; Orca has no notify call, so stops reach the phone as
# the worktree's status comment and Orca's own terminal and agent notifications.


def exe():
    if os.environ.get("ORCA_CLI_COMMAND"):
        return os.environ["ORCA_CLI_COMMAND"]
    if os.environ.get("ORCA_DEV_REPO_ROOT"):
        return "orca-dev"
    return "orca-ide" if sys.platform.startswith("linux") else "orca"


def orca(*args):
    """One Orca CLI call with --json and no shell; its `result`, or Unavailable naming why."""
    found = shutil.which(exe())
    if not found:
        raise Unavailable(f"{exe()} is not on PATH")
    try:
        r = subprocess.run([found, *args, "--json"], capture_output=True, text=True, timeout=30)
        reply = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        raise Unavailable(f"orca {args[0]} {args[1] if len(args) > 1 else ''}: {e}")
    if r.returncode or not isinstance(reply, dict) or reply.get("ok") is not True:
        raise Unavailable(f"orca {' '.join(args[:2])} refused: {(r.stderr or r.stdout).strip()[-200:]}")
    return reply.get("result") or {}


# Lanes live outside the repo folder, and Orca lists such a worktree only once it is in the repo's
# importedExternalWorktreePaths. No CLI verb sets that list, so it goes through the RuntimeClient the orca CLI itself
# runs on, under node. One call: argv is the client module, the method and its JSON params.
RPC = """const [mod, method, params] = process.argv.slice(1);
const C = require(mod).RuntimeClient;
if (!C || typeof C.prototype.call !== "function") { console.error("this Orca has no RuntimeClient.call"); process.exit(3); }
Promise.resolve(new C(undefined, undefined, null, null).call(method, JSON.parse(params)))
  .then(() => process.exit(0), e => { console.error(String((e && e.message) || e)); process.exit(1); });"""


def _client():
    if os.environ.get("ORCA_RUNTIME_CLIENT"):
        return os.environ["ORCA_RUNTIME_CLIENT"]
    found = shutil.which(exe())
    here = os.path.dirname(os.path.realpath(found)) if found else ""
    for base in (here, os.path.dirname(here)):  # the CLI sits in Resources/ or Resources/bin/ beside app.asar.unpacked
        mod = os.path.join(base, "app.asar.unpacked", "out", "cli", "runtime-client.js")
        if base and os.path.isfile(mod):
            return mod
    raise Unavailable("Orca's runtime-client.js was not found beside its CLI; set ORCA_RUNTIME_CLIENT to it")


def _rpc(method, params):
    node = shutil.which("node")
    if not node:
        raise Unavailable("node is not on PATH; Orca needs it to list lanes kept outside the repo folder")
    try:
        r = subprocess.run([node, "-e", RPC, _client(), method, json.dumps(params)], capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(f"orca {method}: {e}")
    if r.returncode:
        raise Unavailable(f"orca {method} refused: {(r.stderr or r.stdout).strip()[-200:]}")


def _same(a, b):
    return bool(a and b) and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _imports(root, lane, keep):
    """Add the lane to (keep) or drop it from the repo's imported worktrees, keeping every other path there."""
    repos = orca("repo", "list")
    repos = repos.get("repos", []) if isinstance(repos, dict) else repos
    repo = next((r for r in repos if _same(r.get("path"), str(root))), None)
    if repo is None:
        raise Unavailable(f"Orca does not list the repo {root}")
    had = list(repo.get("importedExternalWorktreePaths") or [])
    want = [p for p in had if not _same(p, str(lane))] + ([str(lane)] if keep else [])
    if want != had:
        # The whole list is replaced, never appended to, so it is read fresh from Orca each time.
        _rpc("repo.update", {"repo": f"path:{root}", "updates": {"importedExternalWorktreePaths": want}})


def reachable():
    try:
        return bool(orca("status").get("runtime", {}).get("reachable"))
    except Unavailable:
        return False


def _sel(path):
    return f"path:{path}"


def _terminal(where, title, command):
    # One terminal per title per worktree: a rerun of the loop finds it instead of opening another.
    have = orca("terminal", "list", "--worktree", _sel(where)).get("terminals", [])
    if not any(t.get("title") == title for t in have):
        orca("terminal", "create", "--worktree", _sel(where), "--title", title, "--command", command)


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def _listed(lane, bound=10.0):
    """Orca lists a just-imported lane a few seconds late and refuses to set it before; wait, bounded, for its path."""
    end = time.monotonic() + bound
    while not any(_same(p, str(lane)) for p in _strings(orca("worktree", "list"))):
        if time.monotonic() >= end:
            raise Unavailable(f"Orca did not list the lane {lane} within {bound:.0f}s")
        time.sleep(0.5)


def _status(lane, comment):
    orca("worktree", "set", "--worktree", _sel(lane), "--workspace-status", "in-progress", "--comment", comment[:500])


def event(ctx, c, stage, res, lane):
    """Mirror one journaled stage outcome into Orca; Unavailable when Orca could not show it."""
    if stage == "lane" and res.outcome == "advance":
        orca("repo", "add", "--path", str(ctx.root))
        _imports(ctx.root, lane, keep=True)
        _listed(lane)
        _status(lane, f"{c['id']} {c['title']}: building")
        # --until-landed: the terminal outlives stops, so a rerun that finds it by title still has it following.
        _terminal(lane, f"ade {c['id']}", f"{install.ade_cmd()} watch {c['id']} --until-landed")
    elif res.outcome in ("wait", "needs-judgement"):
        _imports(ctx.root, lane, keep=True)
        _status(lane, f"{c['id']} {res.outcome} at {stage}: {res.note}; next: {res.next}")
    elif res.outcome == "done" and stage == "land":
        _status(ctx.root, f"{c['id']} landed")
        _imports(ctx.root, lane, keep=False)  # the lane is gone; Orca keeps no row for it


def board(ctx):
    """The board in an Orca terminal at the repo root."""
    orca("repo", "add", "--path", str(ctx.root))
    _terminal(ctx.root, "ade cockpit", f"{install.ade_cmd()} cockpit --follow --mode cli")


def terminal(ctx, label, cmd):
    """One terminal titled label at the repo root, in Orca."""
    _terminal(ctx.root, label, cmd)

