# id: ade.verbs.digest
# what: one short summary of a day: cards landed, cards parked with their open question, questions whose default applied; then any rule 7 lines (at most 3)
# cmd: ade digest [--day YYYY-MM-DD]
# reads: .ade/records.jsonl
# writes: -
import re
from datetime import date, datetime, timedelta

from ade import config, gitx, records
from ade.refuse import refuse
from ade.verbs import budget, decide

MAX = 10


def add(p):
    p.add_argument("--day", help="the day to summarise, YYYY-MM-DD (default today)")


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


def run(args, cwd):
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
    print("\n".join(out) if out else f"nothing on {iso}")
    return 0
