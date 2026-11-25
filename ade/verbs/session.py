# id: ade.verbs.session
# what: run an ade verb detached from this terminal (no tmux, any platform), list sessions live or ended, follow one, stop one
# cmd: ade session start NAME -- VERB [ARGS] | ade session list | ade session attach NAME [--follow] | ade session stop NAME
# reads: .ade/records.jsonl (session rows), .ade/sessions/<name>.log, processes
# writes: .ade/records.jsonl (session rows), .ade/sessions/<name>.log, an Orca terminal per session in Orca mode
import os
import re
import signal
import subprocess
import sys
import time

from ade import config, install, display, records, route, wait
from ade.config import KIT
from ade.refuse import refuse


def add(p):
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("start", help="run one ade verb detached, its output in .ade/sessions/NAME.log")
    s.add_argument("name")
    s.add_argument("argv", nargs="+", help="the ade verb and its arguments, after --: conduct c3 c4")
    sub.add_parser("list", help="every session, live or ended")
    a = sub.add_parser("attach", help="follow a session's log until it ends (Ctrl-C leaves it running)")
    a.add_argument("name")
    a.add_argument("--poll", type=float, default=1.0)
    a.add_argument("--follow", action="store_true", help="when it ends, wait for the next session of that name and follow that too")
    t = sub.add_parser("stop", help="end a session")
    t.add_argument("name")


def _latest(ctx):
    out = {}
    for r in records.read(ctx, "session"):
        out[r["name"]] = r
    return out


def _get(ctx, name):
    s = _latest(ctx).get(name)
    if s is None:
        refuse("session.args", f"no session {name}", "list them with `ade session list`")
    return s


def _start(ctx, name, argv):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        refuse("session.args", f"session name {name!r} is not a plain word", "name it with letters, digits, dot, dash or underscore")
    s = _latest(ctx).get(name)
    if s and wait.alive(s["pid"], s.get("started")):
        refuse("session.running", f"session {name} is already running (pid {s['pid']})",
               f"`ade session attach {name}`, or `ade session stop {name}` first")
    why = route.parses(argv)
    if why:
        refuse("session.args", f"`ade {' '.join(argv)}` does not parse: {why}", "start it with an ade verb line that runs on its own")
    log = ctx.state / "sessions" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # Detached the standard-library way: its own session on macOS and Linux, its own process group with no console
    # on Windows. So closing this terminal (or this Claude Code session) does not end it, and no tmux is needed.
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
              if sys.platform == "win32" else {"start_new_session": True})
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (str(KIT), os.environ.get("PYTHONPATH")) if p)}
    with open(log, "ab") as f:
        proc = subprocess.Popen([sys.executable, "-m", "ade", *argv], cwd=ctx.root, stdin=subprocess.DEVNULL,
                                stdout=f, stderr=subprocess.STDOUT, env=env, **detach)
    records.append(ctx, "session", {"name": name, "pid": proc.pid, "started": wait.started(proc.pid), "argv": argv,
                                    "log": log.relative_to(ctx.root).as_posix()})
    print(f"session {name}: pid {proc.pid}, `ade {' '.join(argv)}`\n  follow: ade session attach {name}")
    if shown := display.on(ctx):
        try:
            display.terminal(ctx, f"ade session {name}", f"{install.ade_cmd()} session attach {name} --follow")
        except display.Unavailable as e:
            print(f"{shown}: not shown ({e}); `ade session attach {name}` follows it here")


def _attach(ctx, s, poll, follow=False):
    log, pos, ended = ctx.root / s["log"], 0, False
    while True:
        if ended:
            new = _latest(ctx).get(s["name"])
            if new and new != s:  # a rerun: same log file, appended, so its start is where we stopped
                if ctx.root / new["log"] != log:
                    pos = 0
                s, log, ended = new, ctx.root / new["log"], False
        if not ended:
            if log.is_file():
                with open(log, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                pos += len(chunk)
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
            if not (log.is_file() and log.stat().st_size > pos) and not wait.alive(s["pid"], s.get("started")):
                print(f"\nsession {s['name']} ended")
                if not follow:
                    return 0
                ended = True
        try:
            time.sleep(poll)
        except KeyboardInterrupt:
            return 0


def _stop(s):
    if not wait.alive(s["pid"], s.get("started")):
        print(f"session {s['name']} had already ended")
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(s["pid"]), "/T", "/F"], capture_output=True)
    else:
        os.killpg(s["pid"], signal.SIGTERM)  # its own process group: the verb and any engine it started
    print(f"session {s['name']} stopped")


def run(args, cwd):
    ctx = config.load(cwd)
    if args.action == "start":
        _start(ctx, args.name, args.argv)
    elif args.action == "list":
        for name, s in _latest(ctx).items():
            print(f"{name:<16} {'live' if wait.alive(s['pid'], s.get('started')) else 'ended':<6} pid {s['pid']:<7} {s['at']}  ade {' '.join(s['argv'])}")
    elif args.action == "attach":
        return _attach(ctx, _get(ctx, args.name), args.poll, args.follow)
    else:
        _stop(_get(ctx, args.name))
    return 0
