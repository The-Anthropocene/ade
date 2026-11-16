# id: ade.herdr
# what: the herdr adapter: show lanes as herdr tabs with a terminal following each card, and each stop as herdr agent state; never drives
# cmd: -
# reads: ade.toml [cockpit], the herdr CLI's JSON replies
# writes: herdr's view only (the repo's workspace, tabs, panes, agent state); nothing in the repo
import json
import shutil
import subprocess
import sys
import time

from ade import install
from ade.orca import Unavailable

# herdr is a display: only its CLI is called (never its socket), it is never installed or required, and every failure is
# an Unavailable that the caller prints as one note. The calls follow the herdr CLI's documented verbs.


def herdr(*args, parse=True):
    """One herdr CLI call with no shell; its parsed JSON reply (or None), or Unavailable naming why."""
    found = shutil.which("herdr")
    if not found:
        raise Unavailable("herdr is not on PATH")
    try:
        r = subprocess.run([found, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(f"herdr {' '.join(args[:2])}: {e}")
    if r.returncode:
        raise Unavailable(f"herdr {' '.join(args[:2])} refused: {(r.stderr or r.stdout).strip()[-200:]}")
    if not parse:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        raise Unavailable(f"herdr {' '.join(args[:2])}: unreadable reply ({e})")


def reachable():
    try:
        reply = herdr("status", "server", "--json")
        return isinstance(reply, dict) and bool(reply.get("running"))
    except Unavailable:
        return False


def _rows(reply, key):
    # A list of dicts, bare or under key (or under a `result` wrapper).
    if isinstance(reply, dict):
        reply = reply.get(key, reply.get("result", reply))
        if isinstance(reply, dict):
            reply = reply.get(key, [])
    return [x for x in reply if isinstance(x, dict)] if isinstance(reply, list) else []


def _id(row, kind):
    return row.get(f"{kind}_id") or row.get("id")


def _workspace(root):
    if not reachable():
        raise Unavailable("the herdr server is not running")
    label = f"ade {root.name}"
    same = [w for w in _rows(herdr("workspace", "list"), "workspaces") if w.get("label") == label]
    found = next((w for w in same if w.get("focused") is True), same[0] if same else None)
    if len(same) > 1:
        print(f"herdr: {len(same)} workspaces are labelled '{label}' ({', '.join(str(_id(w, 'workspace')) for w in same)}); ade uses {_id(found, 'workspace')}", file=sys.stderr)
    if found is None:
        made = herdr("workspace", "create", "--cwd", str(root), "--label", label, "--no-focus")
        found = made.get("workspace", made.get("result", made)) if isinstance(made, dict) else {}
        if isinstance(found, dict) and "workspace" in found:
            found = found["workspace"]
    ws = _id(found, "workspace") if isinstance(found, dict) else None
    if not ws:
        raise Unavailable("herdr gave no workspace id")
    return ws


def _pane(ws, label):
    """The pane of the tab labelled label in workspace ws, or None when there is no such tab."""
    tab = next((_id(t, "tab") for t in _rows(herdr("tab", "list", "--workspace", ws), "tabs") if t.get("label") == label), None)
    if tab is None:
        return None
    pane = next((p for p in _rows(herdr("pane", "list", "--workspace", ws), "panes") if p.get("tab_id") == tab), None)
    return _id(pane, "pane") if pane else None


def terminal(root, label, cmd, cwd=None):
    """One tab per label per repo workspace: a rerun finds it instead of opening another. Returns its pane id."""
    ws = _workspace(root)
    pane = _pane(ws, label)
    if pane is None:
        herdr("tab", "create", "--workspace", ws, "--cwd", str(cwd or root), "--label", label, "--no-focus", parse=False)
        pane = _pane(ws, label)
        if pane is None:
            raise Unavailable(f"herdr made no pane for {label}")
        herdr("pane", "run", pane, cmd, parse=False)
    return pane


def event(ctx, c, stage, res, lane):
    """Report one journaled stage outcome as the agent state of the card's 'ade cN' pane."""
    label = f"ade {c['id']}"
    if stage == "lane" and res.outcome == "advance":
        state, msg = "working", None
        pane = terminal(ctx.root, label, f"{install.ade_cmd()} watch {c['id']} --until-landed", lane)
    elif res.outcome in ("wait", "needs-judgement"):
        state, msg = "blocked", f"{res.outcome} at {stage}: {res.note}; next: {res.next}"
        pane = terminal(ctx.root, label, f"{install.ade_cmd()} watch {c['id']} --until-landed", lane)
    elif res.outcome == "done" and stage == "land":
        state, msg, pane = "idle", None, _pane(_workspace(ctx.root), label)
    else:
        return
    if pane:
        extra = ["--message", msg[:500]] if msg else []
        herdr("pane", "report-agent", pane, "--source", "ade", "--agent", "ade", "--state", state,
              "--seq", str(int(time.time() * 1000)), *extra, parse=False)


def board(ctx):
    terminal(ctx.root, "ade cockpit", f"{install.ade_cmd()} cockpit --follow --mode cli")
