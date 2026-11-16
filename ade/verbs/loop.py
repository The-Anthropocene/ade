# id: ade.verbs.loop
# what: drive a card through lane, build, review, test, land; journal each stage, stop at the first non-advance. Pilots: run it as a background task, and keep working: the flight band (Claude Code) or the cockpit terminal shows each stage, and the background task's own finish is the one wake; safe because the lane builds in its own worktree and branch, tests run in a detached checkout, and land waits only if the main checkout has uncommitted edits to the very paths the card changes or if the main checkout is not on the trunk branch, so do not edit a flying card's paths and keep the main checkout on trunk
# cmd: ade loop CARD [--model ROLE=ENGINE:MODEL] [--effort ROLE=LEVEL] [--hold-land] [--json]
# reads: .ade/records.jsonl, lane, graph
# writes: .ade/records.jsonl (journal rows and stage records), lane, main branch, .ade/lanes/CARD.lock (the running pid), .ade/lanes/CARD.lock.lk (its lock)
import contextlib
import json
import os

from ade import config, host, orca, providers, records, stages, wait
from ade.refuse import Refusal, refuse
from ade.verbs import decide


def add_flags(p):
    p.add_argument("--model", action="append", metavar="ROLE=ENGINE:MODEL", help="this run only: run ROLE on ENGINE with MODEL (repeatable)")
    p.add_argument("--effort", action="append", metavar="ROLE=LEVEL", help="this run only: ROLE's reasoning effort on its engine (repeatable)")


@contextlib.contextmanager
def flight(args):
    """Carry the verb's --model/--effort flags to every config load of this run (and its lanes' sessions) by env."""
    fl = {k: getattr(args, k) for k in ("model", "effort") if getattr(args, k, None)}
    if getattr(args, "hold_land", False):
        fl["hold"] = args.card
    old = os.environ.get("ADE_FLIGHT")
    os.environ.update({"ADE_FLIGHT": json.dumps(fl)} if fl else {})
    try:
        yield
    finally:
        os.environ.pop("ADE_FLIGHT", None) if old is None else os.environ.update(ADE_FLIGHT=old)


def add(p):
    p.add_argument("card")
    p.add_argument("--theirs", action="store_true", help="run the other machine's card when an id was renumbered")
    add_flags(p)
    p.add_argument("--hold-land", action="store_true", help="this run only: stop at land and ask a person, as a [land] ask path does")
    p.add_argument("--json", action="store_true", help="end the stop with one JSON line: card, stage, outcome, next")


def stop_line(ctx, card, stage, outcome, nxt):
    """The one JSON line a stop ends with. A stop for judgement also carries the open question nearest its deadline."""
    row = {"card": card, "stage": stage, "outcome": outcome, "next": nxt}
    qs = decide.queue(ctx) if outcome in ("needs-judgement", "parked") else []
    if qs:
        row.update(question=qs[0]["text"], recommendation=qs[0].get("rec"), default=qs[0].get("default"),
                   deadline=qs[0].get("deadline"))
    return json.dumps(row)


QUIET = False  # set while the conductor runs a loop in-process, so a conduct run says the bill once


def billing(ctx):
    # Says which bill claude lanes run on; reads config only, never the key file. Silent when no role runs claude.
    if not QUIET and "claude" in {providers.engine(ctx, r) for r in ctx.cfg["roles"]}:
        key = "ANTHROPIC_API_KEY" in ctx.cfg["providers"].get("claude", {}).get("secrets", {})
        print("claude lanes: " + ("API key" if key else "login"))


@contextlib.contextmanager
def held(ctx, card):
    """One run per card: the lock file holds the running pid and its start time, checked and written under
    host.lock. A holder that is dead (or whose pid was reused) is taken over; a nested run in this process passes."""
    p = ctx.state / "lanes" / f"{card}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    lk = p.with_name(p.name + ".lk")  # lock a sibling: Windows locks are mandatory, the data file must stay readable
    me = os.getpid()
    with host.lock(lk):
        try:
            pid, since = json.loads((p.read_text() if p.exists() else "") or "null") or (0, None)
        except ValueError:
            pid, since = 0, None
        if pid and pid != me and wait.alive(pid, since):
            refuse("lane-in-use", f"card {card} is already running in pid {pid}",
                   f"wait for it to end, or `ade wait exit {pid}`")
        mine = pid == me
        if not mine:
            p.write_text(json.dumps([me, wait.started(me)]))
    try:
        yield
    finally:
        if not mine:
            with host.lock(lk):
                p.write_text("")


def run(args, cwd):
    with flight(args):
        ctx = config.load(cwd)
        with held(ctx, stages.card(ctx, args.card, args.theirs)["id"]):
            return _run(args, cwd)


def _run(args, cwd):
    ctx = config.load(cwd)
    c = stages.card(ctx, args.card, args.theirs)
    if c["status"] == "landed":  # no lane, no engine call: the land stage only re-ships a cut-off pr-mode push
        res = stages.FUNCS["land"](ctx, c)
        print(f"{c['id']} {res.note}")
        return 0
    billing(ctx)
    orca.strip(ctx)
    for stage in stages.STAGES:
        try:
            res = stages.FUNCS[stage](ctx, c)
        except Refusal as r:
            # A refusal inside a stage is a stop for judgement, journaled like any other outcome.
            res = stages.Result("needs-judgement", f"[{r.code}] {r.cause}", r.cure)
            records.fired(ctx, r, c["id"])
        records.append(ctx, "journal", {"card": c["id"], "stage": stage, "outcome": res.outcome,
                                        "note": res.note, "next": res.next})
        print(f"{stage:<7} {res.outcome:<16} {res.note}")
        note = orca.event(ctx, c, stage, res, stages.lane_path(ctx, c["id"]))
        if note:
            print(note)
        if res.outcome != "advance":
            if res.outcome == "done":
                return 0
            print(f"next: {res.next}")
            if args.json:
                print(stop_line(ctx, c["id"], stage, res.outcome, res.next))
            return 3
