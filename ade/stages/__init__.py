# id: ade.stages
# what: the stage contract (Result, order, dispatch) and the lane helpers every stage shares
# cmd: -
# reads: .ade/records.jsonl, lane worktree
# writes: -
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ade import config, host, records
from ade.gitx import git
from ade.refuse import refuse

STAGES = ("lane", "build", "review", "test", "land")


@dataclass
class Result:
    outcome: str  # advance | wait | needs-judgement | done
    note: str
    next: str = ""


def lane_base():
    return Path(os.environ.get("ADE_WORK") or "~/.ade-work").expanduser()


def lane_path(ctx, card_id):
    # Outside the repo so lanes never show up in its status or its graph; keyed by repo path, and short
    # so Windows path limits are far away.
    return lane_base() / hashlib.sha1(str(ctx.root).encode()).hexdigest()[:8] / card_id


def branch(card_id):
    return f"ade/{card_id}"


def card(ctx, card_id, theirs=False):
    # An id reconcile moved away from this machine's card now names the other machine's: say so, unless asked for.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    moved = [r for r in records.read(ctx, "renumber") if r["from"] == card_id and r["at"] >= cutoff]
    if moved and not theirs:
        r = moved[-1]
        refuse("card.renumbered", f"{card_id} was renumbered to {r['to']}: yours was '{r['title']}', "
               f"the other machine's {card_id} is '{r['theirs']}'",
               f"ade loop {r['to']} for yours, or pass --theirs to run the other machine's card")
    c = records.merged(ctx, "card").get(card_id)
    if c is None:
        refuse("card.unknown", f"no card {card_id}", "list cards with `ade card list`, or add one with `ade card add`")
    return c


def main(ctx):
    return ctx.cfg["git"]["main"]


def probe(ctx):
    """Main's sha now: a card stamps it when written, so its pick can show what moved under it since."""
    r = git(ctx.root, "rev-parse", "-q", "--verify", main(ctx), check=False)
    return r.stdout.strip() or None


def drift(ctx, c):
    # Files under the card's paths that main changed after the card was written; the card may no longer hold.
    if not c.get("probed_at"):
        return []
    r = git(ctx.root, "diff", "--name-only", c["probed_at"], main(ctx), check=False)
    return [] if r.returncode else [p for p in r.stdout.splitlines() if config.matches(p, c["paths"])]


def moved(ctx, c):
    files = drift(ctx, c)
    more = f" (+{len(files) - 5})" if len(files) > 5 else ""
    return f"paths moved since {c['id']} was written ({c['probed_at'][:8]}): {', '.join(files[:5])}{more}" if files else ""


# A name the card's words treat as code: `backticked`, called(), camelCase or snake_case.
_NAME = re.compile(r"`([^`\s]+)`|\b([A-Za-z_][A-Za-z0-9_]*)\(\)|\b([a-z]+[A-Z][A-Za-z0-9]*|[a-z0-9]+_[a-z0-9_]+)\b")


def premise(ctx, c, root):
    """The card's code-like names that none of its existing files contain. A card is a person's sentence; this is
    the cheap check that it still describes the code (a real port's first card named a function that was not there)."""
    files = [root / p for p in c["paths"] if not any(ch in p for ch in "*?[") and (root / p).is_file()]
    if not files:
        return []
    text = "".join(host.read(f, errors="replace") for f in files)
    names = {next(g for g in m.groups() if g) for m in _NAME.finditer(f"{c['title']} {c['done']}")}
    return sorted(n for n in names if n not in text and "/" not in n and "." not in n)


def tree(lane):
    return git(lane, "rev-parse", "HEAD^{tree}")


def cached(ctx, rtype, c, tree, result):
    return any(r["card"] == c["id"] and r["tree"] == tree and r["result"] == result for r in records.read(ctx, rtype))


def touched(ctx, c):
    return git(lane_path(ctx, c["id"]), "diff", "--name-only", f"{main(ctx)}...HEAD").splitlines()


def row_line(row):
    # A stage's journal row has stage, outcome and note; any other row (a wait) is shown from the fields it has.
    if all(k in row for k in ("stage", "outcome", "note")):
        return f"{row['stage']} {row['outcome']}: {row['note']}"
    return " ".join(f"{k}={v}" for k, v in row.items() if k not in ("type", "at"))


# The stage modules import the helpers above, so they load after them; one file per stage.
from ade.stages import build, land, lane, review, test  # noqa: E402

FUNCS = {"lane": lane.run, "build": build.run, "review": review.run, "test": test.run, "land": land.run}
