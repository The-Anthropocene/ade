# id: ade.display
# what: the display switch: which cockpit display (Orca or herdr) mirrors the run, the dispatchers to it, and the flight strip
# cmd: -
# reads: ade.toml [cockpit]; the orca and herdr adapters
# writes: .ade/cockpit.command (macOS launcher), .ade/cockpit.pid (launch claim); nothing else in the repo
import json
import os
import shutil
import subprocess
import sys
import time

from ade import install, parts, wait
from ade.lib import agent


class Unavailable(Exception):
    pass


def on(ctx):
    """The display to mirror into, "orca" or "herdr", or None: named by "orca"/"herdr" mode; "auto" probes Orca, then
    herdr (asked once a run)."""
    mode = ctx.cfg.get("cockpit", {}).get("mode", "cli")
    off = parts.off(ctx.cfg)
    if mode == "auto" and not hasattr(ctx, "display"):
        from ade import herdr, orca
        ctx.display = "orca" if "orca" not in off and orca.reachable() else "herdr" if "herdr" not in off and herdr.reachable() else None
    shown = ctx.display if mode == "auto" else mode if mode in ("orca", "herdr") else None
    return None if shown in off else shown


def event(ctx, c, stage, res, lane):
    """Mirror one journaled stage outcome into the display. Returns a note to print when it could not show it."""
    shown = on(ctx)
    if not shown:
        return None
    from ade import herdr, orca
    try:
        (herdr if shown == "herdr" else orca).event(ctx, c, stage, res, lane)
    except Unavailable as e:
        return f"{shown}: not shown ({e}); the CLI board has it: ade cockpit"
    return None


def board(ctx):
    """`ade cockpit --mode orca|herdr`: the same board, in a terminal of the chosen display at the repo root."""
    from ade import herdr, orca
    return (herdr if on(ctx) == "herdr" else orca).board(ctx)


def terminal(ctx, label, cmd):
    """One terminal titled label at the repo root, in the chosen display."""
    from ade import herdr, orca
    if on(ctx) == "herdr":
        return herdr.terminal(ctx.root, label, cmd)
    orca.terminal(ctx, label, cmd)


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
    from ade import orca
    band = ctx.cfg.get("cockpit", {}).get("band", "terminal")
    engine = agent.answerer(env).get("engine")
    if os.environ.get("ADE_PROVIDER") == "fake" if env is None else env.get("ADE_PROVIDER") == "fake":
        return  # a rehearsal (or a test) never opens a terminal
    if band == "off" or not (band == "terminal" or engine in ("codex", "opencode")):
        return
    command = f"{install.ade_cmd()} cockpit --follow --mode cli"
    try:
        if on(ctx) == "herdr" or orca.reachable():
            return board(ctx)
        pidfile = ctx.root / ".ade" / "cockpit.pid"
        try:
            word = pidfile.read_text().split()
            if word[0] == "launching":  # a fresh claim counts as open: terminals and python start slowly
                if time.time() - float(word[1]) < 120:
                    return
            elif wait.alive(int(word[0])):
                return
        except (OSError, ValueError, IndexError):
            pass
        try:
            pidfile.write_text(f"launching {int(time.time())}")
        except OSError:
            pass
        if not (launch or _open)(ctx, command):
            pidfile.unlink(missing_ok=True)  # nothing opened: no claim
            print(f"cockpit: no terminal found; run in another one: {command}")
    except (Unavailable, OSError, subprocess.SubprocessError) as e:
        print(f"cockpit: not opened ({e}); run in another terminal: {command}")
