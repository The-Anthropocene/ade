# id: ade.verbs.digest
# what: one short summary of a day: cards landed, cards parked with their open question, questions whose default applied; then any rule 7 lines (at most 3)
# cmd: ade digest [--day YYYY-MM-DD] [--idle HOURS]
# reads: .ade/records.jsonl
# writes: -
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

from ade import config, gitx, records
from ade.refuse import refuse
from ade.verbs import budget, decide

MAX = 10


def add(p):
    p.add_argument("--day", help="the day to summarise, YYYY-MM-DD (default today)")
    p.add_argument("--idle", nargs="?", const="3", help="print the idle signal as JSON; exit 1 when no journal row in the last HOURS (default 3)")


def idle(ctx, hours=3.0):
    """The idle signal: (idle, newest journal row's time or None, hours since it or None, open card ids). Idle = no journal row in the last `hours`."""
    since = max((r["at"] for r in records.read(ctx, "journal")), default=None)
    ago = (datetime.now(timezone.utc) - datetime.fromisoformat(since)).total_seconds() / 3600 if since else None
    opened = [c["id"] for c in records.merged(ctx, "card").values() if c["status"] == "open"]
    return ago is None or ago >= hours, since, None if ago is None else round(ago, 2), opened


def idle_line(ctx, hours=3.0):
    """One line when the board has been idle for `hours`, else ''. Only reports."""
    hit, since, _, _ = idle(ctx, hours)
    return f"idle: no card in flight for {hours:g}h (since {since or 'never'})" if hit else ""


def reverted(ctx, since):
    """{card id: short sha} for each landing that trunk reverted since `since` and not landed again after: by git's default
    'Revert "land cN: ..."' subject, or by a 'This reverts commit <sha>' body line naming a card's land commit."""
    log = gitx.git(ctx.root, "log", "--since", since, "--format=%H %h %cI%n%B%x00", "--grep", '^Revert "land c',
                   "--grep", "^This reverts commit ", check=False).stdout
    rows = records.read(ctx, "card", archive=True)
    landed = {r["id"]: r["at"] for r in rows if r.get("status") == "landed"}  # rows are in order: the last wins
    lands = {r["commit"]: r["id"] for r in rows if r.get("status") == "landed" and r.get("commit")}
    hits = []
    for entry in filter(str.strip, log.split("\0")):
        head, _, body = entry.strip().partition("\n")
        full, sha, at = head.split(" ")
        m = re.match(r'Revert "land (c\d+):', body)
        card = m.group(1) if m else None
        for t in re.findall(r"^This reverts commit ([0-9a-f]{7,40})", body, re.M):
            card = card or next((c for k, c in lands.items() if k.startswith(t)), None)
        if card:
            hits.append((sha, at, card))
    return {c: sha for sha, at, c in reversed(hits)  # git lists newest first, so the newest revert of a card wins
            if c not in landed or datetime.fromisoformat(at) > datetime.fromisoformat(landed[c])}


def waved(ctx):
    """One warning line per [land] ask glob whose last 5+ answered land questions were all an agent's plain 'land';
    a person's answer or a rework ends the run. A line only: nothing stops or changes."""
    qs = [d for d in records.merged(ctx, "decision").values() if d.get("rec") == "land" and "answer" in d]
    lines = []
    for g in ctx.cfg["land"]["ask"]:
        mine = [d for d in qs if any(config.matches(p, [g]) for p in d.get("paths") or [])]
        n = 0
        for d in reversed(mine):
            if d.get("by") != "agent" or d["answer"].lower().split(":")[0].strip() != "land":
                break
            n += 1
        if n >= 5:
            lines.append(f"{g}: an agent answered land the last {n} times; make it a ruling (`ade decide --path`) "
                         "or take it out of [land] ask in ade.toml")
    return lines


def run(args, cwd):
    if args.idle is not None:
        try:
            hours = float(args.idle)
            if not 0 <= hours < float("inf"):
                raise ValueError
        except ValueError:
            print(f"ade: cannot read HOURS {args.idle!r}; use --idle 3 (a number of hours, 0 or more)", file=sys.stderr)
            return 2
        hit, since, ago, opened = idle(config.load(cwd), hours)
        print(json.dumps({"idle": hit, "since": since, "hours": ago, "open": opened}))
        return 1 if hit else 0
    try:
        day = date.fromisoformat(args.day) if args.day else records.today()
    except ValueError:
        refuse("digest.day", f"cannot read day {args.day!r}", "use --day 2026-11-02")
    ctx = config.load(cwd)
    iso, yesterday = day.isoformat(), (day - timedelta(days=1)).isoformat()
    rev = reverted(ctx, iso + " 00:00")
    out = [f"{'reverted' if r['id'] in rev else 'landed'} {r['id']}" for r in records.read(ctx, "card", archive=True)
           if r.get("status") == "landed" and r["at"][:10] == iso]
    qs = [d for d in records.merged(ctx, "decision").values() if "deadline" in d]
    # A park is dated by when its question was filed: an over-share park's deadline is the reset's eve, not today.
    filed = {r["id"] for r in records.read(ctx, "decision", archive=True) if r.get("card") and r["at"][:10] == iso}
    out += [f"parked {d['card']}: {decide.line(d, day)}" for d in qs if d["id"] in filed]
    out += [f"defaulted {decide.line(d, day)}" for d in qs
            if decide.state(d, day) == "defaulted" and d["deadline"] == yesterday]
    if len(out) > MAX:
        out = out[:MAX - 1] + [f"...and {len(out) - MAX + 1} more"]
    outs = [r for r in records.read(ctx, "outcome", archive=True) if r["at"][:10] == iso]
    n_proofs = sum(len(r.get("proofs", [])) for r in outs)
    pins = [(f, n) for r in outs for f, n in r.get("pins", {}).items()]
    if n_proofs or pins:
        out.append(f"evidence: {n_proofs} proofs run across {sum(1 for r in outs if r.get('proofs'))} cards; {sum(n for _, n in pins)} literal pins noted in {len({f for f, _ in pins})} files")
    out += [l for l in budget.fired(ctx).splitlines() if l.startswith("rule 7:")][:3]  # a stop that keeps firing on accepted work
    out += waved(ctx)
    out = out or [f"nothing on {iso}"]
    if line := idle_line(ctx):
        out.append(line)
    print("\n".join(out))
    return 0
