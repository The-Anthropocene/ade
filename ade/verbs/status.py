# id: ade.verbs.status
# what: one-line state for the Claude Code status line: repo, open cards, last stage outcome
# cmd: ade status [--line]
# reads: git worktree list and status of each lane, .ade/records.jsonl, .ade/grep-notes.jsonl, the status-line JSON Claude Code sends on stdin (rate_limits), .ade/lanes/CARD.lock (a run that died: "cut off: cN ...")
# writes: $ADE_WORK/meters.jsonl (Claude's five-hour and weekly use, when the status line is handed them)
import json
import sys
import time
from pathlib import Path

from ade import config, gitx, parts, records, wait
from ade.refuse import Refusal
from ade import telemetry as telemetry_core
from ade.verbs import card, cockpit, decide, digest, telemetry


def add(p):
    p.add_argument("--line", action="store_true", help="status-line form; never fails")


def run(args, cwd):
    try:
        ctx = config.load(cwd)
    except Refusal:
        if args.line:
            print("ade: not initialised")
            return 0
        raise
    if args.line and not sys.stdin.isatty():
        # Claude's only honest source of its own limits: never ~/.claude.json (credentials, and a cache seen frozen
        # for days) and never a usage endpoint. Absent before the first response and on logins without a plan.
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            data = {}
        if isinstance(data, dict) and isinstance(data.get("rate_limits"), dict):
            cost = (data.get("cost") or {}).get("total_cost_usd") if isinstance(data.get("cost"), dict) else None
            telemetry_core.note(ctx, "claude", "status line", data["rate_limits"], session=data.get("session_id"),
                                cost=cost)
    print(line(ctx))
    if not args.line:
        for row in lanes(ctx):
            print(row)
        for c, sha in digest.reverted(ctx, "7 days ago").items():
            if records.merged(ctx, "card").get(c, {}).get("status") == "landed":
                print(f"{c} landed, then reverted on trunk at {sha}")
        note = grep_notes(ctx)
        if note:
            print(note)
        for row in cut_off(ctx):
            print(row)


def cut_off(ctx):
    cards = records.merged(ctx, "card")
    for p in sorted((ctx.state / "lanes").glob("*.lock")):
        try:
            pid, since = json.loads(p.read_text() or "null") or (0, None)
        except ValueError:  # a half-written lock reads as no holder, as ade loop treats it
            continue
        if pid and not wait.alive(pid, since) and cards.get(p.stem, {}).get("status") in ("open", "parked"):
            at = [r["stage"] for r in records.read(ctx, "journal") if r.get("card") == p.stem] or [f"started {since}"]
            yield f"cut off: {p.stem} (run {pid} died at {at[-1]}): ade loop {p.stem}"


def age(seconds):
    s = max(0, int(seconds))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def lanes(ctx):
    """One line per ade/* lane worktree: card id, age since the folder was made, dirty or clean."""
    rows, path = [], None
    for ln in gitx.git(ctx.root, "worktree", "list", "--porcelain", check=False).stdout.splitlines() + [""]:
        if ln.startswith("worktree "):
            path = ln[9:]
        elif ln.startswith("branch refs/heads/ade/") and path:
            card = ln[len("branch refs/heads/ade/"):]
            try:
                born = (Path(path) / ".git").stat().st_mtime  # written when the worktree is made, untouched after
            except OSError:
                continue
            dirty = "dirty" if gitx.git(path, "status", "--porcelain", check=False).stdout.strip() else "clean"
            rows.append(f"  {card} · {age(time.time() - born)} · {dirty}")
    return rows


def grep_notes(ctx):
    """'grep notes: N noted, A acted on (P%): R opened the file, T opened a named test' from the hook's counter; ''
    when missing, empty or unreadable. A note is joined to reads by its lane (worktree top; the cwd or session on an old row)."""
    shown, read, tests = set(), set(), {}
    try:
        text = (ctx.state / "grep-notes.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
            lane = str(row.get("top") or row.get("cwd") or row["session"])
            if isinstance(row.get("shown"), list):
                shown.update((lane, str(f)) for f in row["shown"])
                t = row.get("tests") or []  # {file: [tests]}; a list on an old row is pooled over its files
                for f in row["shown"]:
                    tests.setdefault((lane, str(f)), set()).update(
                        str(x) for x in (t.get(f, []) if isinstance(t, dict) else t))
            elif isinstance(row.get("read"), str):
                read.add((lane, row["read"]))
        except (ValueError, KeyError, TypeError):
            continue  # a malformed row is skipped
    if not shown:
        return ""
    opened = {s for s in shown if s in read}
    named = {s for s in shown if s not in opened and any((s[0], t) in read for t in tests.get(s, ()))}
    n, a = len(shown), len(opened | named)
    return (f"grep notes: {n} noted, {a} acted on ({round(100 * a / n)}%): {len(opened)} opened the file, "
            f"{len(named)} opened a named test")


def line(ctx):
    """The one-line state: repo, open cards, questions waiting, the last stage, the week meters."""
    shown = [card.shown(ctx, c) for c in records.merged(ctx, "card").values()]
    journal = records.read(ctx, "journal")
    last = f" · last {journal[-1]['card']} {cockpit.stage_outcome(journal[-1])}" if journal else ""
    qs = len(decide.queue(ctx)) + len(decide.waiting(ctx))
    meters = telemetry.meter_text(ctx)
    off = sorted(parts.off(ctx.cfg))  # overrides stay visible
    return (f"ade · {ctx.root.name} · {shown.count('open')} open{' · %d held' % shown.count('held') if 'held' in shown else ''}{f' · {qs} to decide: ade decide --desk' if qs else ''}{last}"
            + (f" · off: {', '.join(off)}" if off else "") + (f" · {meters}" if meters else ""))
