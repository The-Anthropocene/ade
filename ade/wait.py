# id: ade.wait
# what: the watcher: wait for one condition with a bound, and read a lane whose log stops growing as stalled, not quiet
# cmd: -
# reads: main checkout, .ade/records.jsonl, .ade/lanes/<card>.log, processes, files
# writes: -
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ade import records
from ade.gitx import git
from ade.refuse import refuse

MET, STOPPED, BOUND, STALLED = 0, 3, 4, 5
SEEN = {}  # the journal row a `stage` wait ended on, for the verb to print
_DUR = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)$")


def seconds(text):
    m = _DUR.match(str(text).strip())
    if not m:
        refuse("wait.args", f"cannot read duration {text!r}", "use a number with s, m, h or d: 90s, 30m, 2h, 1d")
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def started(pid):
    """When a process started, as an opaque string, or None if that cannot be read. A pid alone is not an identity:
    once its process ends the number is handed out again (fast on Windows), so a stored pid is checked against this."""
    import subprocess
    try:
        if sys.platform == "win32":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            t = [ctypes.c_ulonglong() for _ in range(4)]  # creation, exit, kernel, user as FILETIMEs
            ok = k.GetProcessTimes(h, *map(ctypes.byref, t))
            k.CloseHandle(h)
            return str(t[0].value) if ok else None
        stat = Path(f"/proc/{int(pid)}/stat")
        if stat.is_file():
            return stat.read_text().rsplit(")", 1)[1].split()[19]  # field 22, starttime, counted after the name
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True)
        return r.stdout.strip() or None
    except (OSError, IndexError, ValueError):
        return None


def alive(pid, since=None):
    """Whether a process is still running, on every platform, without signalling it. Given the start time recorded
    with the pid, a different process that has since been given the same number reads as not alive."""
    if since is not None and started(pid) != since:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, but alive
    return True


def _clean(ctx, _arg, _since):
    # The two `wait` stops land returns: the main checkout off trunk, or with uncommitted changes.
    head = git(ctx.root, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    return head == ctx.cfg["git"]["main"] and not git(ctx.root, "status", "--porcelain", "--untracked-files=no")


def _journal(ctx, card, since):
    return any(r["card"] == card and r["at"] >= since for r in records.read(ctx, "journal"))


def _stage(ctx, arg, since):
    # Ends on the first journal row for this card and stage written since the wait began, or on a parked or
    # needs-judgement row for an earlier stage (it will never reach this one). A row with no outcome is not finished.
    card, stage = arg.split()
    order = ("lane", "build", "review", "test", "land")
    earlier = order[:order.index(stage)]
    rows = [r for r in records.read(ctx, "journal") if r["card"] == card and r["at"] >= since and r.get("outcome")]
    row = next((r for r in rows if r["stage"] == stage or (r["stage"] in earlier and r["outcome"] in ("parked", "needs-judgement"))), None)
    SEEN.clear()
    SEEN.update(row or {})
    return row is not None


def _answered(ctx, did, _since):
    d = records.merged(ctx, "decision").get(did)
    if d is None:
        refuse("wait.args", f"no decision {did}", "list them with `ade decide --list`")
    from ade.verbs import decide  # the one rule for whether a question is still open
    return decide.state(d) in ("answered", "defaulted")


def _exit(_ctx, pid, _since):
    return not alive(pid)


def _file(_ctx, path, since):
    # Written or touched since the wait began (whole seconds, as journal times are).
    p = Path(path)
    return p.exists() and datetime.fromtimestamp(int(p.stat().st_mtime), timezone.utc).isoformat() >= since


CONDITIONS = {"clean": _clean, "journal": _journal, "stage": _stage, "answered": _answered, "exit": _exit, "file": _file,
              "time": lambda ctx, arg, since: False}  # time is the bound itself


def stalled(ctx, card, quiet):
    """A lane log that has not grown for `quiet` seconds: the engine may be hung, so this is a reading, not peace."""
    log = ctx.state / "lanes" / f"{card}.log"
    return log.is_file() and time.time() - log.stat().st_mtime > quiet


def until(ctx, cond, arg=None, bound="30m", lane=None, poll=1.0):
    """MET when the condition holds, BOUND when the bound passes first, STALLED when the watched lane goes quiet."""
    check = CONDITIONS.get(cond)
    if check is None:
        refuse("wait.args", f"no condition {cond!r}", f"use one of: {', '.join(CONDITIONS)}")
    if cond == "time":
        bound, arg = arg or bound, None
    if cond == "stage" and (len((arg or "").split()) != 2 or arg.split()[1] not in ("lane", "build", "review", "test", "land")):
        refuse("wait.args", "stage needs a card and a stage", "`ade wait stage cN build` (lane, build, review, test or land)")
    if cond != "clean" and cond != "time" and not arg:
        refuse("wait.args", f"{cond} needs what to watch", f"`ade wait {cond} <card|id|pid|path>`")
    end = time.monotonic() + seconds(bound)
    quiet = seconds(f"{ctx.cfg['watch']['stall']}m")
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    while True:
        if check(ctx, arg, since):
            return STOPPED if cond == "stage" and SEEN["outcome"] not in ("advance", "done") else MET
        if lane and stalled(ctx, lane, quiet):
            return STALLED
        if time.monotonic() >= end:
            return MET if cond == "time" else BOUND
        time.sleep(min(poll, max(0.0, end - time.monotonic())))
