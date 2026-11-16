# id: ade.verbs.graph
# what: regenerate the graph file from headers and imports, or with FILE print that file's briefing from a fresh graph; --check fails if the committed one is stale or the route has drifted
# cmd: ade graph [FILE] [--check] [--route]
# reads: source files
# writes: graph.json
from ade import config, graph, host, route
from ade.refuse import refuse
from ade.verbs import hook


def add(p):
    p.add_argument("file", nargs="?", help="print this file's briefing (repo-relative path); writes nothing")
    p.add_argument("--route", action="store_true", help="print the conductor's route as a Mermaid flowchart; writes nothing")
    p.add_argument("--check", action="store_true", help="fail if the graph file does not match the code")


def _route_mermaid():
    """The loop's stages as the main line; each stop row an edge from its stage (or any stage) to its 'then' node."""
    lines = ["flowchart LR", "  lane --> build --> review --> test --> land --> done", '  anystage -- "same stop twice" --> park']  # the conductor's rule, no route row
    edges = {}
    for r in route.load().get("stop", []):
        edges.setdefault((r.get("stage", "*"), r["then"]), []).append(" ".join(x for x in (r["outcome"], r.get("code")) if x))
    for (stage, then), labels in edges.items():
        src = "anystage((any stage))" if stage == "*" else stage
        lines.append(f'  {src} -- "{", ".join(dict.fromkeys(labels))}" --> {then}')
    again = [t for t in ("retry", "wake") if any(t == k[1] for k in edges)]  # these rerun ade loop
    return "\n".join(lines + [f'  {" & ".join(again)} -. "ade loop again" .-> lane'] * bool(again))


def run(args, cwd):
    if args.route:
        print(_route_mermaid())
        return
    ctx = config.load(cwd)
    # Config comes from the main checkout, but the graph is of the worktree you stand in, so a lane regenerates its own.
    here = config.toplevel(cwd) or ctx.root
    if here != ctx.root and (here / "ade.toml").is_file():  # a lane's own [graph] table; every other table stays the main checkout's
        ctx.cfg["graph"] = config.load_at(here).cfg["graph"]
    g = graph.build(here, ctx.cfg)
    if args.file:
        if args.file not in g["files"] and not (here / args.file).is_file():
            base = args.file.rsplit("/", 1)[-1]
            near = sorted(n for n in g["files"] if n.rsplit("/", 1)[-1] == base)
            print(f"no such file: {args.file}" + (f" (did you mean {', '.join(near[:3])}?)" if near else ""))
            raise SystemExit(1)
        print(hook.briefing(ctx, args.file, g))
        return
    text, out = graph.render(g), here / ctx.cfg["graph"]["out"]
    missing = sorted(n for n, f in g["files"].items() if not f["header"])
    if args.check:
        if not out.is_file() or host.read(out) != text:
            refuse("graph.stale", f"{ctx.cfg['graph']['out']} does not match the code",
                   f"run `ade graph` and commit {ctx.cfg['graph']['out']}")
        bad = route.check(ctx, here)
        for where, what in bad:
            print(f"{where}: {what}")
        if bad:
            refuse("route.unbound", f"{len(bad)} route rows or scripts no longer match the verbs they run",
                   "change each line above to what the verb now takes (or route the new stop), then `ade graph`")
        if g["unreadable"]:
            print(f"{len(g['unreadable'])} files unreadable here; their entries carried from {ctx.cfg['graph']['out']}")
        print(f"graph current: {len(g['files'])} files")
    else:
        if g["unreadable"]:
            refuse("graph.unreadable", f"{len(g['unreadable'])} files cannot be read here: {', '.join(g['unreadable'][:5])}",
                   "run `ade graph` where those files can be read, so a regenerate never drops their edges")
        host.write(out, text)
        print(f"wrote {ctx.cfg['graph']['out']}: {len(g['files'])} files, {sum(len(v) for v in g['callers'].values())} edges")
    # Only where land asks for headers: a ported project's files need none, and 180 names there are noise.
    if "header" in ctx.cfg["graph"]["require"]:
        for n, f in sorted(g["files"].items()):
            if f.get("lacks"):
                print(f"{n}: header lacks {', '.join(f['lacks'])}")
        if missing:
            print(f"without a five-line header: {', '.join(missing)}")
