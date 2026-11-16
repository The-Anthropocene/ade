# id: ade.verbs.parts
# what: list the parts catalogue with each part's state; `set x=off` (or `x=on`) switches one in ade.toml and prints what it costs
# cmd: ade parts [set ID=off|on [--why REASON]]
# reads: ade/parts.toml, ade.toml [parts]
# writes: ade.toml [parts] (set only)
from ade import config, host, parts
from ade.refuse import refuse


def add(p):
    p.add_argument("action", nargs="?", choices=["set"], help="set: switch a part")
    p.add_argument("change", nargs="?", metavar="ID=off|on", help="the part and its new state")
    p.add_argument("--why", default="", help="the reason you go past a warning, kept in [parts.override]")


def run(args, cwd):
    ctx = config.load(cwd)
    cat, now_off = parts.catalogue(), parts.off(ctx.cfg)
    if not args.action:
        for pid, c in cat.items():
            print(f"{'off' if pid in now_off else 'on ':3}  {pid:10} {c['group']:8} {c['what']}")
        return 0
    pid, _, state = (args.change or "").partition("=")
    if pid not in cat or state not in ("on", "off"):
        refuse("parts.set", f"`{args.change}` is not ID=off or ID=on for a known part",
               f"run `ade parts` for the ids, then `ade parts set {next(iter(cat))}=off`")
    why = dict(ctx.cfg["parts"]["override"])
    if state == "off":
        now_off.add(pid)
        for w in parts.warnings(cat, pid, now_off, pid != "orca" or ctx.cfg["cockpit"]["mode"] != "cli"):
            print(f"warning: {w}")
        why.update({pid: args.why} if args.why else {})
    else:
        now_off.discard(pid)
        why.pop(pid, None)
    f = ctx.root / "ade.toml"
    host.write(f, parts.rewrite(host.read(f) if f.is_file() else "", now_off, why))
    print(f"{pid} is {state}; wrote ade.toml")
    return 0
