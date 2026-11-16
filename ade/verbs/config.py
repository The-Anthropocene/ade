# id: ade.verbs.config
# what: list ade.toml's sections, or look up every key matching a word with its effective value and template comment
# cmd: ade config [word] [--roles | --seats | --refusals | --probe [--guards]]
# reads: ade/template.toml, ade.toml
# writes: -
import re
import tomllib

from ade import budget, config, host, providers
from ade.lib import probe

END = "ade.toml is protected: you may run `ade parts set` (and `ade setup`); name any other key to the person, who changes it and reruns `ade init`."


def add(p):
    p.add_argument("word", nargs="?", help="a section, key or comment word, such as model, effort, unit or share")
    p.add_argument("--roles", action="store_true", help="each role's engine, model, effort and guard, with where each came from")
    p.add_argument("--seats", action="store_true", help="per role and engine, the argv, env and settings files a call gets; spends nothing")
    p.add_argument("--refusals", action="store_true", help="every refusal code with its cause and cure, from the source")
    p.add_argument("--probe", action="store_true", help="spend one tiny call per role to check its provider, model and effort answer")
    p.add_argument("--guards", action="store_true", help="with --probe: per engine, ask it to print a canary .env and say whether the secret guard blocked it")


def valid(parts):
    """True when the text before an inline comment is a TOML value, so a prose comment is not mistaken for an example."""
    try:
        tomllib.loads(f"k = {parts[0]}")
        return True
    except tomllib.TOMLDecodeError:
        return False


def parse():
    """The template's sections as (name, [purpose sentence from its "#: " line]) and its keys as (section, key, comment lines), in order."""
    secs, keys, pend, purpose = [], [], [], ""
    for line in host.read(config.TEMPLATE).splitlines():
        s = line.strip()
        if s.startswith("#: "):
            purpose = s[3:]
            continue
        if m := re.fullmatch(r"\[([^\]]+)\]", s):
            secs.append((m[1], [purpose]))
            purpose = ""
        elif (m := re.match(r"#\s*([\w-]+) = (.*)", s)) and valid(m[2].partition(" # ")):
            inline = m[2].partition(" # ")[2]
            keys.append((secs[-1][0], m[1], ["unset; template example"] + ([inline] if inline else [])))
            pend = []
            continue
        elif s.startswith("#"):
            pend = pend + [s.lstrip("# ")]
            continue
        elif m := re.match(r'("[^"]+"|[\w-]+) = (.*)', s):
            inline = m[2].partition(" # ")[2]
            keys.append((secs[-1][0], m[1].strip('"'), pend + ([inline] if inline else [])))
        pend = []
    return secs, keys


def walk(cfg, path):
    for part in path:
        cfg = cfg.get(part) if isinstance(cfg, dict) else None
    return cfg


def listing(secs, keys):
    out = ["ade.toml sections (a section you leave unset takes the kit default):"]
    names = {n for n, _ in secs}
    subs = {}
    for name, _ in secs:
        if "." in name:
            top, _, sub = name.partition(".")
            subs.setdefault(top, []).append(f"[{name}]")
    seen = set()
    for name, doc in secs:
        top = name.partition(".")[0]
        if "." in name:
            if top not in names and top not in seen:
                out.append(f"  [{top}.*]: {', '.join(subs[top])}")
                seen.add(top)
            continue
        out.append(f"  [{name}]  {doc[0]}")
        if name in subs:
            out.append(f"    {', '.join(subs[name])}")
    return out


def origin(text, mine, section, key, sub=None):
    """'ade.toml line N' when ade.toml sets section.key (and sub, for a table key), else 'kit default'."""
    path = section.split(".") + [key] + ([sub] if sub else [])
    if walk(mine, path) is None:
        return "kit default"
    here = None
    for n, line in enumerate(text.splitlines(), 1):
        if m := re.fullmatch(r"\s*\[([^\]]+)\]\s*", line):
            here = m[1]
        elif here == section and re.match(rf'\s*"?{re.escape(key)}"?\s*=', line):
            return f"ade.toml line {n}"
    return "ade.toml"


def roles(ctx, mine, text):
    """One block per [roles.*] entry, read from the loaded config; the template supplies the defaults."""
    out, pcs = [], ctx.cfg["providers"]
    for name, role in ctx.cfg["roles"].items():
        prov, sec = role["provider"], f"roles.{name}"
        pc, psec = ctx.cfg["providers"][prov], f"providers.{prov}"
        rows = [("provider", prov, origin(text, mine, sec, "provider"))]
        for eng in role["models"]:  # every engine, so an opposite-engine reviewer's model and effort show too
            tag = f"{eng}{' (active)' if eng == prov else ''}"
            rows.append((f"model {tag}", role["models"][eng], origin(text, mine, sec, "models", eng)))
            if level := walk(role, ["effort", eng]):
                rows.append((f"effort {tag}", f"{level} (role)", origin(text, mine, sec, "effort", eng)))
            elif eng == "codex" and (level := pcs.get("codex", {}).get("default_effort")):
                rows.append((f"effort {tag}", f"{level} (default_effort)", origin(text, mine, "providers.codex", "default_effort")))
            else:
                rows.append((f"effort {tag}", "unset (engine's own setting)", "kit default"))
            if eng == "codex":
                lim = [f"{k} = {pcs.get('codex', {})[k]}" for k in ("auto_compact_token_limit", "context_window") if pcs.get("codex", {}).get(k)]
                rows.append((f"compaction {tag}", ", ".join(lim) or "unset", "ade.toml" if lim else "inherited from ~/.codex/config.toml"))
        if "lane_settings" in pc:
            rows.append(("guard", f"lane_settings = {pc['lane_settings']}", origin(text, mine, psec, "lane_settings")))
        elif "sandbox" in pc:
            rows.append(("guard", f"sandbox = {pc['sandbox']!r}", origin(text, mine, psec, "sandbox")))
        elif "modes" in pc:
            mode = ctx.cfg["permissions"]["lane_mode"]
            rows.append(("guard", f"modes[{mode}] = {pc['modes'].get(mode)}", origin(text, mine, psec, "modes")))
        out += [f"[{sec}]"] + [f"  {k:<25}{v}  ({w})" for k, v, w in rows]
    return out


def refusal_block(rs, code):
    """A code's heading, then each distinct cause and cure with the file:line of every site that raises it."""
    groups = {}
    for r in rs:
        if r["code"] == code:
            groups.setdefault((r["cause"], r["cure"]), []).append(r["at"])
    return [code] + [l for (cause, cure), at in groups.items()
                     for l in (f"  cause: {cause}", f"  cure:  {cure}", f"    at {', '.join(at)}")]


def run(args, cwd):
    ctx = config.load(cwd)
    secs, keys = parse()
    if args.probe:
        return probe.guards(ctx) if args.guards else probe.roles(ctx, cwd)
    mine = tomllib.loads(host.read(ctx.root / "ade.toml"))
    rs = budget.refusals()
    if args.refusals:
        lines = [l for c in budget.refusal_codes() for l in refusal_block(rs, c)]
    elif args.word in budget.refusal_codes():
        lines = refusal_block(rs, args.word)
    elif args.roles or args.seats:
        lines = providers.seats(ctx) if args.seats else roles(ctx, mine, host.read(ctx.root / "ade.toml"))
    elif not args.word:
        lines = listing(secs, keys)
    else:
        w = args.word.lower()
        lines, also = [], []
        for sec, key, doc in keys:
            if w in f"[{sec}] {key}".lower():
                path = sec.split(".") + [key]
                where = "set in ade.toml" if walk(mine, path) is not None else "kit default"
                lines += [f"[{sec}] {key} = {walk(ctx.cfg, path)!r}  ({where})"] + [f"    {c}" for c in doc]
            elif w in " ".join(doc).lower():
                also.append(f"[{sec}] {key}")
        if not lines and not also:
            print("\n".join([f"refused: no ade.toml key matches {args.word!r}; valid forms: a section, key or comment word"] + listing(secs, keys) + ["", END]))
            return 2
        lines += ["also mentioned in: " + ", ".join(also)] * bool(also)
    print("\n".join(lines + ["", END]))
    return 0
