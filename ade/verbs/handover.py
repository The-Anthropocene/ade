# id: ade.verbs.handover
# what: generate a role's working context from records and graph; prints its token estimate, fails over budget
# cmd: ade handover [--role R]
# reads: ade.toml [roles], .ade/records.jsonl, graph.json, tracked files (git ls-files), roles/<role>.voice.md
# writes: -
from ade import budget, config, graph, host, install, records, stages
from ade.gitx import git
from ade.refuse import refuse
from ade.verbs import decide


def add(p):
    p.add_argument("--role", default="pilot")


def netline(net):
    return " ".join(f"{k} {v:+d}" for k, v in net.items()) or "no line changes"


def next_command(ctx, c, journal):
    rows = [r for r in journal if r["card"] == c["id"]]
    if rows and rows[-1].get("outcome") in ("wait", "needs-judgement"):
        return rows[-1]["next"]
    return f"ade loop {c['id']}"


def flying_lines(ctx):
    """How to fly this project's board, read from its own ade.toml, so the lines hold for whoever installed it."""
    git = ctx.cfg["git"]
    lands = f"lands one at a time on {git['main']}" + (", then pushes" if git["push"] == "land" else "")
    return ["- Questions and discussion are answered in the chat; work that changes files becomes a card first.",
            "- Asked to start the loop: `ade session start board -- conduct` runs it detached (add `--lanes N` for "
            "several cards at once); `ade cockpit` reports where it stands.",
            f"- `ade conduct` flies every open card in turn; `ade conduct --lanes N` flies up to N at once, each in its "
            f"own lane, and each {lands}.",
            "- `ade card list --free` names the cards that can fly now: their reach in the graph meets no card already "
            "flying. To fly them yourself, `ade session start lane-CARD -- conduct CARD` for each; `ade session list` "
            "shows what is flying.",
            "- Work kept outside the board (TODO lines, issues, a backlog file) becomes cards: one `ade card add TITLE "
            "--path GLOB --done TEXT` per job, its paths as narrow as the job.",
            "- A failure showing EPERM or a denied read on a secret-like file (*.key, .env*) may come from your own "
            "[guards] sandbox, not the product: rerun the failing test through ade (the lane's test stage) before "
            "blaming it."]


def short(d):
    return f"- {d['id']} [{', '.join(d['paths'])}]: {decide.first_sentence(d['text'])} {decide.by(d)}"


def decision_lines(ctx):
    """Path-free rulings in full, newest first, within half the budget; path-bound rulings and answered questions one line each, newest first, within a quarter."""
    done = {c for c, r in records.merged(ctx, "card").items() if r.get("status") in ("landed", "closed")}
    rows = [d for d in records.merged(ctx, "decision").values() if decide.state(d) not in ("open", "retired") and not d.get("standing")
            and not (d.get("card") in done and d.get("kind") == "question")]  # a landed card's questions stay in `ade decide --list`
    rows.reverse()
    budget_ = ctx.cfg["budgets"]["handover_tokens"]
    free = [d for d in rows if not d["paths"] and decide.state(d) == "ruling"]
    rest = [d for d in rows if d not in free]
    out = []
    for group, room, fmt, what in ((free, budget_ // 2, lambda d: f"- {decide.line(d)}", "path-free rulings"),
                                   (rest, budget_ // 4, short, "path-bound rulings and answered questions")):
        used = 0
        for i, d in enumerate(group):
            used += len(fmt(d)) // 4 + 1
            if used > room:
                out.append(f"- {len(group) - i} more {what}: `ade decide --list`, one in full with `ade decide --show ID`")
                break
            out.append(fmt(d))
    return out


def text(ctx, name):
    """The role's working context as one block of markdown; the SessionStart hook injects the same text."""
    role = ctx.cfg["roles"].get(name)
    if role is None:
        refuse("handover.refused", f"no role {name} in ade.toml", f"use one of: {', '.join(ctx.cfg['roles'])}")
    cards, journal = records.merged(ctx, "card"), records.read(ctx, "journal")
    out = [f"# ADE handover: {name}", "", install.READ_SKILL, "", role["what"], ""]
    stand = [d for d in records.merged(ctx, "decision").values() if d.get("standing") and decide.state(d) == "ruling"]
    if stand:
        out += ["## Standing"] + [f"- {d['id']} {d['text']}" for d in stand] + [""]
    voice = ctx.root / "roles" / f"{name}.voice.md"
    if voice.is_file():
        out += ["## Voice", host.read(voice).strip(), ""]
    out += ["## Open cards"]
    for c in (c for c in cards.values() if c["status"] == "open"):
        last = next((r for r in reversed(journal) if r["card"] == c["id"]), None)
        state = stages.row_line(last) if last else "not started"
        out += [f"- {c['id']} {c['title']} [{', '.join(c['paths'])}] done: {c['done']}",
                f"  state: {state}; next: `{next_command(ctx, c, journal)}`"]
    if name == "pilot":
        out += ["", "## Flying the board", *flying_lines(ctx)]
    out += ["", "## Questions waiting on a person"] + [f"- {decide.line(d)}" for d in decide.queue(ctx)]
    if name == "pilot" and (w := decide.waiting_line(ctx)):
        out += [w]
    out += ["", "## Decisions"] + decision_lines(ctx)
    if name == "pilot":
        files = set(git(ctx.root, "ls-files").splitlines())
        out += [f"- {d['id']} matches no file: retire?" for d in records.merged(ctx, "decision").values()
                if decide.state(d) == "ruling" and d["paths"] and not any(config.matches(f, d["paths"]) for f in files)]
    out += ["", "## Recent landings"] + [f"- {o['card']} {o['commit'][:10]} net {netline(o['net'])}"
                                         for o in records.read(ctx, "outcome")[-10:]]
    g = graph.load(ctx.root, ctx.cfg)
    if g:
        hubs = sorted(g["callers"].items(), key=lambda kv: -len(kv[1]))[:8]
        out += ["", f"## Graph ({len(g['files'])} files; each edit is briefed from it)"]
        out += [f"- {n}: {(g['files'][n]['header'] or {}).get('what', '')} ({len(v)} callers)" for n, v in hubs]
    out += ["", "## Configuring ADE", "Settings live in ade.toml; unset ones take the kit default; find any with `ade config WORD`. "
            "ade.toml is protected: you may run `ade parts set` (and `ade setup`); name any other key to the person, who changes it and reruns `ade init`."]
    if not budget.off(ctx):
        out += ["", "## Budgets", budget.render(budget.measure(ctx))]
    return "\n".join(out)


def run(args, cwd):
    ctx = config.load(cwd)
    body = text(ctx, args.role)
    tokens, limit = len(body) // 4, ctx.cfg["budgets"]["handover_tokens"]
    print(body + f"\n\n~{tokens} tokens (budget {limit})")
    if tokens > limit:
        refuse("handover.refused", f"the {args.role} handover is ~{tokens} tokens, over {limit}",
               "land or close cards and supersede old decisions so the board shrinks, then rerun")
