# id: ade.verbs.card
# what: add, list, show, hold, close, snooze and re-path cards; a card needs paths and a done line, and a recurring one opens again every N days
# cmd: ade card add TITLE --path GLOB --done TEXT [--every DAYS] [--proof CMD] [--hold REASON] [--profile NAME] [--why TEXT] | ade card draft SENTENCE | ade card list [--free] | ade card show ID | ade card hold ID REASON | ade card close ID --why TEXT | ade card snooze ID --until DATE|Nd | ade card paths ID --path GLOB [--path GLOB ...] [--add]
# reads: .ade/records.jsonl, ade.toml [roles.<role>.profiles] (the names --profile may take)
# writes: .ade/records.jsonl (card rows; a held card also gets its park question)
import re
import sys
from datetime import date, datetime, timedelta, timezone

from ade import config, graph, providers, q, records, stages
from ade.gitx import git
from ade.refuse import refuse
from ade.verbs import conduct
from ade.verbs import decide
from ade.verbs.decide import when, who

DONE_HELP = ("when it is finished, and the proof: usually a command's output, a page, a figure or a runtime log "
             "(a rehearsal line counts); keep a test only for a lasting hazard")


def add(p):
    sub = p.add_subparsers(dest="action", required=True)
    a = sub.add_parser("add", help="add a card")
    a.add_argument("title")
    a.add_argument("--path", action="append", required=True, help="path or glob the card touches (repeatable)")
    a.add_argument("--done", required=True, help=DONE_HELP)
    a.add_argument("--proof", action="append", metavar="CMD",
                   help="a command land runs itself on the merged main; its exit code and output hash go in the outcome (repeatable)")
    a.add_argument("--every", type=int, metavar="DAYS",
                   help="recurring: after it lands it rests, then opens again DAYS days later (snooze it to put that off)")
    a.add_argument("--hold", metavar="REASON",
                   help="add the card held: a question conduct honours, so it never flies until `ade decide --answer QID retry`")
    a.add_argument("--profile", metavar="NAME",
                   help="the role profile ([roles.<role>.profiles.NAME]) its calls use, over [profiles] select")
    a.add_argument("--why", metavar="TEXT",
                   help="the need, the choice made and what was turned down; shown, never checked")
    d = sub.add_parser("draft", help="have the pilot draft a card from one sentence, held for a veto")
    d.add_argument("sentence")
    ls = sub.add_parser("list", help="list cards")
    ls.add_argument("--free", action="store_true",
                    help="only open cards that can fly now: none flying, and their reach in the graph meets no card "
                         "that is (the cards `ade conduct --lanes` would pick, in order)")
    s = sub.add_parser("show", help="show one card with its latest stage, review and test, and the stops conduct retried")
    s.add_argument("id")
    x = sub.add_parser("close", help="close a card that will not be done, and say why; conduct never flies it")
    x.add_argument("id")
    x.add_argument("--why", required=True, help="the reason, kept with who closed it and when")
    h = sub.add_parser("hold", help="hold an open card: a question conduct honours, answered with `ade decide --answer QID retry`")
    h.add_argument("id")
    h.add_argument("reason")
    pa = sub.add_parser("paths", help="replace an open card's paths; title, done and status stay")
    pa.add_argument("id")
    pa.add_argument("--path", action="append", required=True, help="path or glob the card touches (repeatable)")
    pa.add_argument("--add", action="store_true", help="join the given paths to the card's existing ones instead of replacing them")
    z = sub.add_parser("snooze", help="rest a recurring card until a date; it opens again by itself then")
    z.add_argument("id")
    z.add_argument("--until", required=True, help="YYYY-MM-DD, or Nd")


def draft(ctx, sentence):
    records.fetch(ctx)
    g = graph.load(ctx.root, ctx.cfg) or {"files": {}}
    files = "\n".join(f"{n}: {(f['header'] or {}).get('what', '')}" for n, f in sorted(g["files"].items()))
    t = ctx.cfg["tests"]
    runs = ", ".join(f"`{c}`" for c in [*t["always"], t["command"]] if c)
    prompt = (f"Draft one board card for this sentence: {sentence}\n\nThe repo's files, with what each is for:\n{files}\n\n"
              f"The project's checks run with: {runs}. Name only checks these can run.\n\n"
              "Reply with exactly these lines and nothing else: one `TITLE: ...`, one or more `PATH: ...` (a repo path "
              "or glob from the list, or a new file in a folder the list already has), one `DONE: ...` naming an "
              "observable check, and optionally one or more `PROOF: ...` lines, each a command that shows the done line "
              "holds (one of the checks above or a command run in the repo; `{python}` is allowed).")
    text = providers.call(ctx, "pilot", prompt, ctx.root, {"id": "draft", "title": sentence, "paths": []})
    got = {k: re.findall(rf"^{k}:[ \t]*(.+?)\s*$", text, re.M) for k in ("TITLE", "PATH", "DONE", "PROOF")}
    cure = "rephrase the sentence, or `ade card add` by hand"
    if not (got["TITLE"] and got["PATH"] and got["DONE"]):
        refuse("card.draft", "the pilot's reply lacks a TITLE, a PATH or a DONE line", cure)
    for path in got["PATH"]:
        inside = not path.startswith(("/", "\\")) and ":" not in path and ".." not in path.replace("\\", "/").split("/")
        if not inside or not any(config.matches(n, [path]) for n in g["files"]) \
                and not (ctx.root / path.split("*")[0].rpartition("/")[0]).is_dir():
            refuse("card.draft", f"the pilot named {path}, which is not in the graph and has no folder", cure)
    row = records.append(ctx, "card", {"title": got["TITLE"][0], "paths": got["PATH"], "done": got["DONE"][0],
                                       "status": "open", "probed_at": stages.probe(ctx), "drafted": sentence}
                         | ({"proofs": got["PROOF"]} if got["PROOF"] else {}), prefix="c")
    # Next-morning rule: a draft made at or after midday counts its veto window from tomorrow,
    # so an evening draft with accept = 0 gets about a day, not minutes. Drafts only; decide's deadlines stay.
    now = datetime.now()
    base = now.date() + timedelta(days=1 if now.hour >= 12 else 0)
    deadline = (base + timedelta(days=int(ctx.cfg.get("cards", {}).get("accept", 0)))).isoformat()
    ask = records.append(ctx, "decision", {"text": f"{row['id']} drafted from: {sentence}", "paths": [], "kind": "question",
                                           "card": row["id"], "rec": "accept", "default": "accept",
                                           "deadline": deadline}, prefix="d")
    print(f"{row['id']}  {row['title']}  [{', '.join(row['paths'])}]\n  done: {row['done']}\n"
          + "".join(f"  proof: {p}\n" for p in row.get("proofs", [])) +
          f"accept now: ade decide --answer {ask['id']} accept\nveto: ade decide --answer {ask['id']} drop"
          f" (by {deadline}; it is accepted after that)\n"
          f"redraft: ade decide --answer {ask['id']} \"retry: NOTE\"")


def _state(c):
    return "resting" if c["status"] in ("landed", "closed") and c.get("every") and c.get("due") else c["status"]


def shown(ctx, c):
    return "held" if c["status"] == "open" and conduct.waiting(ctx, c) else _state(c)


def _next(ctx, c):
    """One line of hops from records at read time: open questions, last q handles, the branch; at most 8."""
    hops = [f"{d['id']} {d['text'][:60]} (ade decide --answer {d['id']} ...)"
            for d in records.merged(ctx, "decision").values()
            if d.get("card") == c["id"] and d.get("kind") == "question" and decide.state(d) == "open"]
    with q._use(ctx) as con:
        hops += [f"q{i} {cmd[:40] or 'output'}" for i, cmd in
                 con.execute("SELECT id, cmd FROM outputs WHERE card = ? ORDER BY id DESC LIMIT 8", (c["id"],))]
    if git(ctx.root, "branch", "--list", f"ade/{c['id']}").strip():
        hops.append(f"ade/{c['id']} branch")
    return "next: " + "; ".join(hops[:8]) if hops else ""


def _warn_paths(ctx, paths):
    tracked = git(ctx.root, "ls-files").splitlines()
    for path in paths:
        if not any(config.matches(f, [path]) for f in tracked) and not any(f.startswith(path.rstrip("/") + "/") for f in tracked):
            print(f"warning: {path} matches no tracked file yet; until it exists, conduct keeps this card apart from any lane in "
                  f"{(path.split('*')[0].rpartition('/')[0] + '/').lstrip('/') or 'the whole repo'}", file=sys.stderr)


def _hold(ctx, c, reason):
    from . import decide
    if (open_q := next((d for d in decide.queue(ctx) if d.get("card") == c["id"]), None)):
        print(f"{c['id']}  already held; release: ade decide --answer {open_q['id']} retry")
        return
    held = records.append(ctx, "decision", {"text": f"{c['id']} held: {reason}", "paths": c["paths"],
                                            "kind": "question", "rec": "retry", "card": c["id"]}, prefix="d")
    print(f"{c['id']}  held; release: ade decide --answer {held['id']} retry")


def run(args, cwd):
    ctx = config.load(cwd)
    if args.action == "draft":
        return draft(ctx, args.sentence)
    if args.action == "add":
        names = providers.profiles(ctx)
        if args.profile and args.profile not in names:
            refuse("card.profile", f"no role defines a profile {args.profile!r}",
                   f"use one of: {', '.join(names)}" if names else "define [roles.<role>.profiles.<name>] in ade.toml first")
        records.fetch(ctx)
        _warn_paths(ctx, args.path)
        every = {"every": args.every} if args.every else {}
        every |= {"proofs": args.proof} if args.proof else {}
        every |= {"profile": args.profile} if args.profile else {}
        every |= {"why": args.why} if args.why else {}
        row = records.append(ctx, "card", {"title": args.title, "paths": args.path, "done": args.done,
                                           "status": "open", "probed_at": stages.probe(ctx), **every}, prefix="c")
        if args.hold:
            _hold(ctx, row, args.hold)
        else:
            print(f"{row['id']}  next: ade loop {row['id']}")
        g = graph.load(ctx.root, ctx.cfg) or {"files": {}}
        for path in args.path:
            moved = [y for y, _ in graph.comoves(g, path)]
            if moved:
                print(f"co-moves: {path} -> {', '.join(moved)}")
            hit = next((g for g in ctx.cfg["protected"]["paths"] if config.matches(path, [g]) or config.matches(g, [path])), None)
            if hit:
                cures = ctx.cfg["protected"].get("cures", {})
                cure = next((t for k, t in cures.items() if config.matches(path, [k])), "")
                print(f"warning: {path} is protected ({cure or hit}); a lane cannot change it")
    elif args.action == "hold":
        c = stages.card(ctx, args.id, theirs=True)
        if c["status"] != "open":
            refuse("card.hold", f"{c['id']} is {c['status']}, and only an open card is held", f"ade card show {c['id']}")
        _hold(ctx, c, args.reason)
    elif args.action == "close":
        c = stages.card(ctx, args.id, theirs=True)
        if c["status"] == "landed" and not c.get("every"):
            refuse("card.close", f"{c['id']} is landed, so there is nothing to close", f"ade card show {c['id']}")
        records.append(ctx, "card", {"id": c["id"], "status": "closed", "closed_why": args.why,
                                     "closed_by": who(), "retired": True, "closed_at": date.today().isoformat()})
        decide.drop_lane(ctx, c["id"])
        print(f"{c['id']} closed: {args.why}")
    elif args.action == "paths":
        c = stages.card(ctx, args.id, theirs=True)
        if c["status"] in ("landed", "closed"):
            refuse("card.close", f"{c['id']} is {c['status']}, so its paths are not replaced", f"ade card show {c['id']}")
        _warn_paths(ctx, args.path)
        paths = list(dict.fromkeys([*c["paths"], *args.path] if args.add else args.path))
        records.append(ctx, "card", {"id": c["id"], "paths": paths})
        print(f"{c['id']} paths: {', '.join(paths)}")
    elif args.action == "snooze":
        c, until = stages.card(ctx, args.id, theirs=True), when(args.until)
        if not c.get("every") or until <= date.today():
            refuse("card.snooze", f"{args.id} is not a recurring card, or {until} is not after today",
                   "a one-off card is held (ade card add --hold) or closed (ade card close <id> --why ...)")
        records.append(ctx, "card", {"id": c["id"], "due": until.isoformat()})
        print(f"{c['id']} rests until {until}, then opens again")
    elif args.action == "list":
        cards = list(records.merged(ctx, "card").values())
        if args.free:
            cards = conduct.free(ctx, cards, conduct.flying(ctx))
        for c in cards:
            drifted = c["status"] != "landed" and stages.drift(ctx, c)
            rest = f"  (every {c['every']}d, due {c['due']})" if c.get("every") and c.get("due") else ""
            print(f"{c['id']:<5} {shown(ctx, c):<7} {c['title']}  [{', '.join(c['paths'])}]{'  (moved)' if drifted else ''}{rest}")
    else:
        c = stages.card(ctx, args.id, theirs=True)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        for r in [r for r in records.read(ctx, "renumber") if r["from"] == c["id"] and r["at"] >= cutoff][-1:]:
            print(f"{c['id']} was renumbered: yours is now {r['to']}")
        print(f"{c['id']} {_state(c)}: {c['title']}\n" + (f"why: {c['why']}\n" if c.get("why") else "") + f"paths: {', '.join(c['paths'])}\ndone: {c['done']}")
        if c.get("every"):
            print(f"every {c['every']} days; " + (f"due {c['due']}" if c.get("due") else "open until it first lands"))
        if c["status"] != "landed" and stages.moved(ctx, c):
            print(stages.moved(ctx, c))
        outs = [r for r in records.read(ctx, "outcome") if r["card"] == c["id"]]
        out = outs[-1] if outs else {}
        for p in out.get("proofs", []):
            print(f"proof: {p['cmd']}  rc {p['rc']}  out {p['out_sha256'][:12]}")
        if not out:
            for cmd in c.get("proofs", []):
                print(f"proof (not run yet): {cmd}")
        for f, n in out.get("pins", {}).items():
            print(f"pins noted: {f} +{n}")
        for rtype in ("journal", "review", "test"):
            rows = [r for r in records.read(ctx, rtype) if r["card"] == c["id"]]
            if rows:
                last = rows[-1]
                body = last.get("text") or last.get("tail") or stages.row_line(last)
                if rtype == "test" and last.get("result") == "pass":
                    body = f"{last.get('tests') or 'tests'} passed"  # a passing tail can hold fixture output that reads like a refusal
                print(f"--- last {rtype} ({last.get('result', last.get('outcome'))})\n"
                      + q.emit(ctx, body.strip(), cmd=f"card show {c['id']} {rtype}", card=c["id"]))
                if last.get("patch_file"):
                    print(f"patch_file: {last['patch_file']}")
        for r in records.read(ctx, "journal"):  # the stops conduct retried on the way, one row each
            if (r["card"], r.get("stage"), r.get("outcome")) == (c["id"], "conduct", "retried"):
                print(f"retried {r['at'][:10]}: {r['note']}")
        nxt = _next(ctx, c)
        if nxt:
            print(nxt)
