# id: ade.verbs.conduct
# what: walk the board through `ade loop` until no open card is left untried, one card at a time or several whose reach the graph keeps apart; route every stop by ade/route.toml, wake a model only where the route says wake. Pilots: run it as a background task, and keep working: the flight band (Claude Code) or the cockpit terminal shows each stage, and the background task's own finish is the one wake; safe because each lane builds in its own worktree and branch, tests run in a detached checkout, and land waits only if the main checkout has uncommitted edits to the very paths the card changes or if the main checkout is not on the trunk branch, so do not edit a flying card's paths and keep the main checkout on trunk
# cmd: ade conduct [CARD ...] [--lanes N] [--follow [--rounds N]] [--check] [--model ROLE=ENGINE:MODEL] [--effort ROLE=LEVEL] [--json]
# reads: ade/route.toml, .ade/records.jsonl, usage meters
# writes: .ade/seen and .ade/sessions files older than 30 days (removed), .ade/records.jsonl (hold rows for parked cards, a journal row per stop it retried; the loop writes the rest), lanes and main through ade loop
import contextlib
import os
import re
import shlex
import time
from datetime import date, timedelta

from ade import cli, config, graph, orca, parts, providers, records, route, stages, telemetry, wait
from ade.gitx import git
from ade.refuse import Refusal, refuse
from ade.verbs import decide, loop, session, telemetry as flights

_CODE = re.compile(r"^\[([\w.-]+)\]")


def add(p):
    p.add_argument("cards", nargs="*", help="cards to conduct, in order; none means every open card, oldest first")
    p.add_argument("--theirs", action="store_true", help="run the other machine's card when an id was renumbered")
    p.add_argument("--lanes", type=int, default=1, metavar="N",
                   help="fly up to N cards at once, each in its own session, picking only cards whose reach in the graph "
                        "does not meet a card already flying")
    p.add_argument("--json", action="store_true",
                   help="after each parked card, one JSON line like `ade loop --json` (not with --lanes)")
    p.add_argument("--follow", action="store_true",
                   help="after a run ends, sleep ADE_FOLLOW_POLL seconds (default 60) and run again, so a card added "
                        "meanwhile is flown; stops on a provider over its week share, a new day (unless [conduct] follow_stop is \"never\") or the call cap (not with named cards)")
    p.add_argument("--rounds", type=int, metavar="N", help="with --follow, stop after N rounds")
    loop.add_flags(p)
    p.add_argument("--check", action="store_true", help="only check the route and bound scripts against the verbs")


_THEIRS = False  # this run was given --theirs: the loops it starts are too


def _run(ctx, line, c):
    # A route line is an ade command, run in-process exactly as typed, so the route can only say what the CLI accepts.
    argv = shlex.split(line.replace("{card}", c["id"]))[1:]
    argv += ["--theirs"] if _THEIRS and argv[:1] == ["loop"] else []
    print(f"$ {line.replace('{card}', c['id'])}" + (" --theirs" if argv[-1:] == ["--theirs"] else ""))
    loop.QUIET = True  # the conduct run said the bill already
    try:
        return cli.main(argv, cwd=str(ctx.root))
    finally:
        loop.QUIET = False


def _stop(ctx, c, before):
    # The last journal row the loop wrote for the card in this run (not conduct's own); none if it journaled nothing.
    return next((r for r in reversed(records.read(ctx, "journal")[before:]) if r["card"] == c["id"] and r.get("stage") != "conduct"), None)


def _now(ctx, c):
    """The card as the board holds it now, or None. A landing's records push can renumber the card mid-run (the other
    machine minted its id first), so the old id may name that machine's card: follow ctx.renumbered, else the title."""
    every = records.merged(ctx, "card")
    cid = ctx.renumbered.get(c["id"], c["id"])
    if cid in every and every[cid]["title"] == c["title"]:
        return every[cid]
    same = lambda x: all(x.get(k) == c.get(k) for k in ("title", "paths", "done"))  # title alone can name another card
    return next((x for x in reversed(list(every.values())) if same(x) and x["id"] != c["id"]), None)


def _park(ctx, c, stop, why, deadline=None):
    records.append(ctx, "hold", {"session": "conduct", "card": c["id"], "waits_on": stop["next"] if stop else why,
                                 "kind": "person"})
    # The deadline is today unless given: decide.state defaults a question the day after its deadline, so the next
    # day's run retries it (an over-share park passes the reset's eve, so it flies again on the reset day).
    decide.park(ctx, f"{c['id']} parked: {why}", {"paths": c["paths"], "kind": "question", "rec": "retry",
                 "default": "retry", "deadline": deadline or date.today().isoformat(), "card": c["id"]})
    print(f"parked {c['id']}: {why}" + (f"\n  next: {stop['next']}" if stop and stop.get("next") else ""))
    return "parked"


def _holding(ctx, c):
    """The question waiting() holds the card on, or None: the newest land question until answered land or rework, else the card's newest question while open."""
    qs = [d for d in records.merged(ctx, "decision").values() if d.get("card") == c["id"]]
    lq = [d for d in qs if d.get("rec") == "land"][-1:]  # the newest land question: only 'land' or 'rework: TEXT' passes it
    if lq and not (lq[0].get("answer", "").lower().startswith("land") or stages.land.rework_note(lq[0]) is not None):
        return lq[0]  # open, or answered anything else (a bare 'rework', 'retry'): the card stays down
    qs = lq or qs  # the land wait's park is moot once a person said land or rework
    return qs[-1] if qs and decide.state(qs[-1]) == "open" else None  # any answer, or the default applying, releases the card


def waiting(ctx, c):
    """True while the card's latest parked question is open; any answer or the default applying releases it (a land question wants 'land' or 'rework')."""
    return _holding(ctx, c) is not None


def _wake(ctx, c, stop, role):
    over = [m for m in telemetry.meters(ctx, {providers.engine(ctx, role, card=c)}) if m[3]]
    if over:
        row = next((r for r in reversed(records.read(ctx, "share")) if r["provider"] == over[0][0]), {})
        reset = telemetry._when(row.get("until")) if row.get("until") else None
        eve = (reset.date() - timedelta(days=1)).isoformat() if reset else None
        return f"{over[0][0]} is over its week share ({over[0][1]} of {over[0][2]:.0%})", eve
    lp = stages.lane_path(ctx, c["id"])
    prompt = (f"You are the ADE {role}, woken by the conductor because card {c['id']} ({c['title']}) stopped at "
              f"{stop['stage']}: {stop['note']}\nThe next command it printed: {stop['next']}\n"
              f"Do what that stop needs, in {lp if lp.is_dir() else ctx.root}, then end; the conductor reruns the loop. "
              f"Only edits inside the card's paths ({', '.join(c.get('paths') or [])}) are committed for the rerun, and an "
              f"edit anywhere else stops the landing; if the fix needs a file outside them, record `ade decide --ask` "
              f"naming that file and end without editing it. "
              f"If a person must decide, record it with `ade decide --ask ... --card {c['id']}` and end without changing anything.")
    providers.call(ctx, role, prompt, lp if lp.is_dir() else ctx.root, c)
    if lp.is_dir() and c.get("paths"):
        # A lane runs without git for the pilot, so commit its fix for the rerun; only the card's paths.
        # Tracked changes only, named by diff: a card path git does not know yet would fail add -u.
        changed = git(lp, "diff", "--name-only", "HEAD", "--", *c["paths"]).splitlines()
        out = ctx.cfg["graph"]["out"]
        if changed and (lp / out).is_file():
            # The rerun's build stage says "already built", so the fix's new edges reach the graph only here.
            stages.build.regenerate(ctx, lp)
            changed = sorted(set(changed) | set(git(lp, "diff", "--name-only", "HEAD", "--", out).splitlines()))
        if changed:
            git(lp, "add", "--", *changed)
            git(lp, "commit", "-q", "-m", f"{c['id']}: pilot fix after {stop['stage']} stop", "--", *changed)
    return None, None


_ENDED = set()  # (pid, start time) of sessions already seen to have ended


def flying(ctx):
    """Cards in the air: those a live session is looping or conducting. Derived from sessions, never stored."""
    out = set()
    for s in session._latest(ctx).values():
        key = (s["pid"], s.get("started"))
        if key in _ENDED or s["pid"] == os.getpid() or s["argv"][0] not in ("loop", "conduct"):
            continue
        if not wait.alive(*key):
            if key[1] is not None:  # a pid with its start time is never the same process again: stop re-checking it
                _ENDED.add(key)
            continue
        out.update(a for a in s["argv"][1:] if re.fullmatch(r"c\d+", a))  # card ids only; the session running this is not another flight
    return out


def free(ctx, cards, busy):
    """The open cards that can fly now: in board order, skipping any whose reach meets a card in `busy`, and each
    card picked also counts as busy for the ones after it."""
    g = graph.load(ctx.root, ctx.cfg) or {"files": {}, "callers": {}, "tests": {}}
    prose, every = ctx.cfg["rigour"]["no_review"], records.merged(ctx, "card")
    taken = [graph.reach(g, every[b]["paths"], prose) for b in busy if b in every]
    out = []
    for c in cards:
        if c["status"] in ("landed", "closed") or c["id"] in busy:
            continue
        r = graph.reach(g, c["paths"], prose)
        if not any(graph.clash(r, t) for t in taken):
            out.append(c)
            taken.append(r)
    return out


def _reap():
    # The sessions this run starts are its own children: reap the finished ones, or they linger as zombies whose
    # pid still reads as alive. Windows keeps no zombies, and has no WNOHANG.
    with contextlib.suppress(ChildProcessError):
        while hasattr(os, "WNOHANG") and os.waitpid(-1, os.WNOHANG)[0]:
            pass


def crash(ctx, cid):
    """The exception line when the card's lane log ends in a Python traceback, else None."""
    try:
        lines = (ctx.state / "sessions" / f"lane-{cid}.log").read_text(errors="replace").rstrip().splitlines()
    except OSError:
        return None
    tb = [i for i, x in enumerate(lines) if x.startswith("Traceback (most recent call last)")]
    # sessions append to the log across runs: only a traceback running unbroken to the last line counts, not a stale one
    body = lines[tb[-1] + 1:-1] if tb else []
    return lines[-1] if body and all(x[:1].isspace() for x in body) and not lines[-1][:1].isspace() else None


_ANSWERED = {}  # card -> the answered questions it had when its lane last started: a newer answer flies it again


def _answers(ctx, cid):
    return {k for k, d in records.merged(ctx, "decision").items() if d.get("card") == cid and d.get("answer")}


def lanes(ctx, ids, n, tried=None):
    """Keep up to n cards flying, each as its own detached `ade conduct CARD` session; when one ends, pick again.
    Each card is tried once per run, so the run ends when nothing is flying and nothing untried is free."""
    tried, refused = set() if tried is None else tried, set()
    every = records.merged(ctx, "card")
    named = {i: every[i]["title"] for i in ids or () if i in every}  # a landing can renumber an open card mid-run
    air, beat = {}, time.time()
    while True:
        every = records.merged(ctx, "card")
        for i, t in list(named.items()):
            if i not in every or every[i]["title"] != t:  # follow the title to the open card that now carries it
                new = next((c["id"] for c in every.values() if c["title"] == t and c["status"] not in ("landed", "closed")), None)
                if new and new not in named:
                    if i in tried:  # already flown under the old id: the card is the same, so it stays tried
                        tried.add(new)
                    del named[i]
                    named[new] = t
        ids = list(named) if named else ids
        board = [every[i] for i in ids if i in every] if ids else list(every.values())
        board = [c for c in board if c["status"] != "closed" and (ids or not waiting(ctx, c))]  # one named by hand flies regardless
        _reap()
        up = flying(ctx)
        again = lambda c: c["id"] in tried and not waiting(ctx, c) and _answers(ctx, c["id"]) - _ANSWERED.get(c["id"], set())
        for c in free(ctx, [c for c in board if c["id"] not in tried or again(c)], up)[:max(0, n - len(up))]:
            tried.add(c["id"])
            _ANSWERED[c["id"]] = _answers(ctx, c["id"])
            try:
                session._start(ctx, f"lane-{c['id']}", ["conduct", c["id"]] + (["--theirs"] if _THEIRS else []))
                up.add(c["id"])
            except Refusal as r:  # say, a live session already holds this lane's name: skip the card, fly the rest
                print(r.render())
                refused.add(c["id"])
                records.fired(ctx, r, c["id"])
        if not up:
            break
        now = time.time()
        air = {i: air.get(i, now) for i in up}
        if now - beat >= 600:  # heartbeat: each flying lane, how long up and how long since its log last grew
            beat = now
            for i in sorted(up):
                try:
                    idle = (now - (ctx.state / "sessions" / f"lane-{i}.log").stat().st_mtime) / 60
                except OSError:
                    idle = (now - air[i]) / 60
                print(f"flying {i}: {(now - air[i]) / 60:.0f} min in the air, log last grew {idle:.0f} min ago" + (" (quiet)" if idle > 20 else ""))
        time.sleep(2)
    done = {i: c["status"] if c["status"] in ("landed", "closed") else "parked" for i, c in records.merged(ctx, "card").items() if i in tried}
    for i, v in done.items():
        if v == "parked" and (why := crash(ctx, i)):
            done[i] = "crashed"
            print(f"crashed {i}: {why}\n  next: ade session attach lane-{i}")
    for i in named or ids or ():
        if i not in tried or i in refused:
            c = records.merged(ctx, "card").get(i)
            why = ("not on the board" if c is None else c["status"] if c["status"] in ("landed", "closed")
                   else "its session start was refused" if i in refused else "held by a clash with a card still flying")
            print(f"not flown {i}: {why}")
    return _end(ctx, done)


def _end(ctx, done):
    # The end line: each card's verdict, and the stops conduct retried for it since its latest flight (journal rows).
    since, rows = {f["card"]: f["at"] for f in records.read(ctx, "flight")}, records.read(ctx, "journal")
    said = {i: "; ".join(r["note"] for r in rows if (r["card"], r.get("stage"), r.get("outcome")) == (i, "conduct", "retried")
                         and r["at"] >= since.get(i, "")) for i in done}
    for k, v in done.items():  # a land whose push failed is still landed, but the person should see it here
        land = next((r for r in reversed(rows) if r["card"] == k and r.get("stage") == "land" and r.get("outcome") == "done"), None)
        if v == "landed" and land and "not pushed (see above)" in land.get("note", ""):
            done[k] = "landed, not pushed"
    print("conducted: " + (", ".join(f"{k} {v}" + (f" (retried: {said[k]})" * bool(said[k])) for k, v in done.items()) or "nothing open"))
    return 0 if all(v.startswith(("landed", "closed")) for v in done.values()) else 3


def conduct(ctx, c, rt):
    """Drive one card to landed, closed or parked. Returns that word."""
    wakes, last, line = 0, None, rt["start"]["run"]
    while True:
        before = len(records.read(ctx, "journal"))
        _run(ctx, line, c)
        if (now := _now(ctx, c)) is None:  # gone from its id and carried by no other: write nothing
            print(f"lost {c['id']}: renumbered mid-run and not found again")
            return "parked"
        c = now
        stop = _stop(ctx, c, before)
        if c["status"] in ("landed", "closed"):
            return c["status"]
        if stop is None or stop["outcome"] in ("advance", "done"):
            return _park(ctx, c, stop, "the loop ended without a stop the route can read")
        m = _CODE.match(stop["note"])
        row = route.pick(rt.get("stop", []), stop["stage"], stop["outcome"], m and m[1])
        key = (stop["stage"], stop["outcome"], stop["note"])
        if row is None or row["then"] == "park":
            return _park(ctx, c, stop, "the route parks this stop")
        if key == last:
            return _park(ctx, c, stop, f"the same stop twice, after {row['then']}")
        last, line = key, row["run"]
        if row["then"] == "wait":
            code = None if waiting(ctx, c) else wait.until(ctx, row["wait"], None, ctx.cfg["conduct"]["wait"], c["id"] if stop["stage"] in ("build", "review") else None)
            if code != wait.MET:
                return _park(ctx, c, stop, "a question is open, and time clears no question" if code is None else f"waited {ctx.cfg['conduct']['wait']} on {row['wait']} (exit {code})")
        elif row["then"] == "wake":
            if wakes >= ctx.cfg["conduct"]["wakes"]:
                return _park(ctx, c, stop, f"woke a model {wakes} times for this card already")
            wakes += 1
            why, eve = _wake(ctx, c, stop, row["role"])
            if why:
                return _park(ctx, c, stop, why, eve)
            if waiting(ctx, c):  # the woken pilot asked a person: that question holds the card, a second would double the ask
                q = _holding(ctx, c)["id"]
                records.append(ctx, "hold", {"session": "conduct", "card": c["id"], "waits_on": q, "kind": "person"})
                print(f"held {c['id']}: waits on {q} (asked by the woken pilot)")
                return "parked"
        did = {"wait": f"waited on {row.get('wait')}", "wake": f"woke the {row.get('role')}"}.get(row["then"], "ran again")
        records.append(ctx, "journal", {"card": c["id"], "stage": "conduct", "outcome": "retried", "next": line.replace("{card}", c["id"]),
                                        "note": f"{stop['stage']} {stop['outcome']}: {stop['note'].partition(chr(10))[0]}, {did}"})


def run(args, cwd):
    with loop.flight(args), flights.flight(ctx := config.load(cwd)):  # loaded once loop.flight has set the env
        return _fly(ctx, args, cwd)


def _sweep(ctx, days=30):
    """Remove session leftovers (.ade/seen, .ade/sessions) untouched for `days`, lane logs (lane-*.log) excepted; whatever cannot be removed stays, silently."""
    old = time.time() - days * 86400
    for sub in ("seen", "sessions"):
        with contextlib.suppress(OSError):
            for f in (ctx.state / sub).iterdir():
                with contextlib.suppress(OSError):
                    if f.is_file() and not f.name.startswith("lane-") and f.stat().st_mtime < old:
                        f.unlink()


def _fly(ctx, args, cwd):
    if "conductor" in parts.off(ctx.cfg):
        refuse("conduct.off", "the conductor part is off in ade.toml [parts]",
               "fly cards one at a time with `ade loop CARD`, or switch it on with `ade parts set conductor=on`")
    bad = route.check(ctx, config.toplevel(cwd) or ctx.root)
    if bad or args.check:
        for where, what in bad:
            print(f"{where}: {what}")
        if bad:
            refuse("route.unbound", f"{len(bad)} route rows or scripts no longer match the verbs they run",
                   "change each line above to what the verb now takes (or route the new stop), then `ade graph`")
        print("route and scripts bound: every line parses, every stop is routed")
        return 0
    if args.follow and args.cards:
        refuse("conduct.follow", "--follow watches the board, so it cannot take named cards",
               "drop the card names, or drop --follow")
    loop.billing(ctx)
    orca.strip(ctx)
    _sweep(ctx)
    if not args.follow:
        return _round(ctx, args, set(), {})
    tried, done, day, n = set(), {}, date.today(), 0
    while True:
        code = _round(ctx, args, tried, done)
        n += 1
        over = [m for m in telemetry.meters(ctx, {providers.engine(ctx, r) for r in ctx.cfg["roles"]}) if m[3]]
        why = (f"{over[0][0]} is over its week share" if over else
               capped(ctx) or
               ("a new day began" if ctx.cfg["conduct"]["follow_stop"] == "day" and date.today() != day else None) or
               (f"{args.rounds} rounds done" if args.rounds and n >= args.rounds else None))
        if why:
            print(f"following stopped: {why}")
            return code
        time.sleep(float(os.environ.get("ADE_FOLLOW_POLL", 60)))


def capped(ctx):
    """Why the engine-call cap stops the run, or None. Calls are ADE's own usage rows: today's, and the last 7 days'."""
    cap, today = ctx.cfg["conduct"]["cap"], date.today()
    days = [r["at"][:10] for r in records.read(ctx, "usage")]
    for name, span in (("day", 1), ("week", 7)):
        n = sum(d > (today - timedelta(days=span)).isoformat() for d in days)
        if cap.get(name) and n >= cap[name]:
            return f"the call cap for the {name} ({cap[name]}) is reached"


def _round(ctx, args, tried, done):
    # One run over the board. `tried` (lanes) and `done` (one lane) carry across rounds of a follow session.
    global _THEIRS
    _THEIRS = args.theirs or not args.cards  # a board pick names the card meant; only a typed id can be stale
    if args.lanes > 1 and not capped(ctx):
        return lanes(ctx, args.cards, args.lanes, tried)
    rt = route.load()
    while True:
        # With no cards named, the board is read again after every card, so one added mid-run (by a person or a woken
        # model) is flown in the same run; each card is tried once, and a parked one waits for the next run.
        todo = [stages.card(ctx, i, args.theirs) for i in args.cards] if args.cards else [
            c for c in records.merged(ctx, "card").values() if c["status"] != "landed"]
        c = next((c for c in todo if c["status"] != "closed" and c["id"] not in done and c["id"] not in tried and (args.cards or not waiting(ctx, c))), None)
        if c is None:
            break
        if capped(ctx):
            print(f"stopped: {capped(ctx)}")
            break
        print(f"--- {c['id']}: {c['title']}")
        before = len(records.read(ctx, "journal"))
        records.append(ctx, "flight", {"flight": os.environ["ADE_FLIGHT_ID"], "card": c["id"]})
        tried.add(c["id"])  # the id this run started: after a renumbering it names the other machine's card
        verdict = conduct(ctx, c, rt)
        c = _now(ctx, c) or c  # report the id the card carries after the run, never the old one
        done[c["id"]] = verdict
        if args.json and done[c["id"]] == "parked":
            stop = _stop(ctx, c, before) or {}
            print(loop.stop_line(ctx, c["id"], stop.get("stage"), "parked", stop.get("next")))
    return _end(ctx, done)
