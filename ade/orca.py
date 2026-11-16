# id: ade.orca
# what: the Orca adapter: show lanes as Orca worktrees with a terminal following each card, and the board; never drives
# cmd: -
# reads: ade.toml [cockpit], $ORCA_CLI_COMMAND / $ORCA_DEV_REPO_ROOT / $ORCA_RUNTIME_CLIENT, orca's JSON replies
# writes: Orca's view only (repo, its imported lanes, worktree status and comment, terminals); .ade/cockpit.command (macOS launcher); nothing else in the repo
import json
import os
import shutil
import subprocess
import sys

from ade import install
from ade.lib import agent

# Orca is the glass cockpit display: everything here is best-effort, and a failure is a printed note, never a stop.
# The calls are settled against Orca 1.4.232; Orca has no notify call, so stops reach the phone as
# the worktree's status comment and Orca's own terminal and agent notifications.


class Unavailable(Exception):
    pass


def exe():
    if os.environ.get("ORCA_CLI_COMMAND"):
        return os.environ["ORCA_CLI_COMMAND"]
    if os.environ.get("ORCA_DEV_REPO_ROOT"):
        return "orca-dev"
    return "orca-ide" if sys.platform.startswith("linux") else "orca"


def on(ctx):
    """The display to mirror into, "orca" or "herdr", or None: named by "orca"/"herdr" mode; "auto" probes Orca, then
    herdr (asked once a run)."""
    mode = ctx.cfg.get("cockpit", {}).get("mode", "cli")
    if mode == "auto" and not hasattr(ctx, "display"):
        from ade import herdr
        ctx.display = "orca" if reachable() else "herdr" if herdr.reachable() else None
    return ctx.display if mode == "auto" else mode if mode in ("orca", "herdr") else None


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


def _status(lane, comment):
    orca("worktree", "set", "--worktree", _sel(lane), "--workspace-status", "in-progress", "--comment", comment[:500])


def event(ctx, c, stage, res, lane):
    """Mirror one journaled stage outcome into Orca. Returns a note to print when Orca could not show it."""
    shown = on(ctx)
    if not shown:
        return None
    try:
        if shown == "herdr":
            from ade import herdr
            herdr.event(ctx, c, stage, res, lane)
        elif stage == "lane" and res.outcome == "advance":
            orca("repo", "add", "--path", str(ctx.root))
            _imports(ctx.root, lane, keep=True)
            _status(lane, f"{c['id']} {c['title']}: building")
            # --until-landed: the terminal outlives stops, so a rerun that finds it by title still has it following.
            _terminal(lane, f"ade {c['id']}", f"{install.ade_cmd()} watch {c['id']} --until-landed")
        elif res.outcome in ("wait", "needs-judgement"):
            _imports(ctx.root, lane, keep=True)
            _status(lane, f"{c['id']} {res.outcome} at {stage}: {res.note}; next: {res.next}")
        elif res.outcome == "done" and stage == "land":
            _status(ctx.root, f"{c['id']} landed")
            _imports(ctx.root, lane, keep=False)  # the lane is gone; Orca keeps no row for it
    except Unavailable as e:
        return f"{shown}: not shown ({e}); the CLI board has it: ade cockpit"
    return None


def board(ctx):
    """`ade cockpit --mode orca|herdr`: the same board, in a terminal of the chosen display at the repo root."""
    if on(ctx) == "herdr":
        from ade import herdr
        return herdr.board(ctx)
    orca("repo", "add", "--path", str(ctx.root))
    _terminal(ctx.root, "ade cockpit", f"{install.ade_cmd()} cockpit --follow --mode cli")


def terminal(ctx, label, cmd):
    """One terminal titled label at the repo root, in the chosen display."""
    if on(ctx) == "herdr":
        from ade import herdr
        return herdr.terminal(ctx.root, label, cmd)
    _terminal(ctx.root, label, cmd)


def _alive(pid):
    if sys.platform == "win32":  # os.kill would terminate the process there
        argv = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
        out = subprocess.run(argv, capture_output=True, text=True).stdout
        return any(field == f'"{pid}"' for line in out.splitlines() for field in line.split(","))
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _open(ctx, command):
    """Open one OS terminal running command; a truthy name of what it used, or None when none could be found."""
    names = [n for _, ns in agent.MARKERS for n in ns]  # the window is the person's: no engine marker reaches it
    env = {k: v for k, v in os.environ.items() if k not in names}
    if sys.platform == "darwin":
        script = ctx.root / ".ade" / "cockpit.command"
        script.write_text(f"#!/bin/sh\nunset {' '.join(names)}\ncd {json.dumps(str(ctx.root))} && {command}\n")
        script.chmod(0o755)
        argv = ["open", "-a", "Terminal", str(script)]
    elif sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # the root goes in cwd, never into a shell string
        subprocess.Popen(f'cmd /k "title ade cockpit && {command}"', cwd=ctx.root, creationflags=flags, env=env)
        return "start"
    else:
        term = shutil.which("x-terminal-emulator")
        if not term or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return None
        argv = [term, "-e", "sh", "-c", f"unset {' '.join(names)}; {command}"]  # a shared terminal server ignores our env
    proc = subprocess.Popen(argv, cwd=ctx.root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None if sys.platform == "darwin" and proc.wait() else argv[0]  # 'open' returns at once; a sandbox can fail it


def strip(ctx, env=None, launch=None):
    """At the start of a loop or conduct: show the flight strip in one cockpit terminal for a pilot that has no band.
    [cockpit] band: "terminal" (default, every pilot), "auto" (only Codex/OpenCode), "off". Orca first, else a live .ade/cockpit.pid, else one new OS terminal. Never raises, never stops."""
    band = ctx.cfg.get("cockpit", {}).get("band", "terminal")
    engine = agent.answerer(env).get("engine")
    if os.environ.get("ADE_PROVIDER") == "fake" if env is None else env.get("ADE_PROVIDER") == "fake":
        return  # a rehearsal (or a test) never opens a terminal
    if band == "off" or not (band == "terminal" or engine in ("codex", "opencode")):
        return
    command = f"{install.ade_cmd()} cockpit --follow --mode cli"
    try:
        if on(ctx) == "herdr" or reachable():
            return board(ctx)
        try:
            if _alive(int((ctx.root / ".ade" / "cockpit.pid").read_text().strip())):
                return
        except (OSError, ValueError):
            pass
        if not (launch or _open)(ctx, command):
            print(f"cockpit: no terminal found; run in another one: {command}")
    except (Unavailable, OSError, subprocess.SubprocessError) as e:
        print(f"cockpit: not opened ({e}); run in another terminal: {command}")
