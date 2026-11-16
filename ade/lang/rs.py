# id: ade.lang.rs
# what: Rust reader: `mod x;` and `use crate/self/super::` paths resolved to the deepest existing module file of the crate (each Cargo.toml is a crate root); files beside src/ import the crate's lib.rs
# cmd: -
# reads: -
# writes: -
import re

EXTS = (".rs",)
_MOD = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.M)
_USE = re.compile(r"\buse\s+([^;]+);")
_ROOTS = ("mod.rs", "lib.rs", "main.rs")


def _crate(name, crates):
    best = None
    for c in crates:
        if (c == "" or name.startswith(c + "/")) and (best is None or len(c) > len(best)):
            best = c
    return best


def _module(name, crate):
    # Module path of a file under <crate>/src, or None when it is not part of the module tree.
    src = f"{crate}/src/" if crate else "src/"
    if not name.startswith(src):
        return None
    parts = name[len(src):-3].split("/")
    if len(parts) == 1 and parts[0] in ("lib", "main"):
        return ()
    return tuple(parts[:-1] if parts[-1] == "mod" else parts)


def prepare(root, names):
    crates = sorted(n[: -len("Cargo.toml")].rstrip("/") for n in names if n.split("/")[-1] == "Cargo.toml")
    mods = {}
    for n in sorted(names, reverse=True):  # lib.rs sorts before main.rs, so it is written last and wins
        if n.endswith(".rs"):
            c = _crate(n, crates)
            m = _module(n, c) if c is not None else None
            if m is not None:
                mods[(c, m)] = n
    return {"names": set(names), "crates": crates, "mods": mods}


def _expand(s):
    # `a::{b, c::d}` -> a::b, a::c::d (nested groups too); `*` and `as` tails are dropped.
    s = re.sub(r"\s+", "", re.sub(r"\s+as\s+\w+", "", s))
    out = []

    def walk(text, prefix):
        depth, cur, items = 0, "", []
        for ch in text:
            if ch == "," and depth == 0:
                items.append(cur)
                cur = ""
                continue
            depth += ch == "{"
            depth -= ch == "}"
            cur += ch
        items.append(cur)
        for it in items:
            if "{" in it:
                head, _, rest = it.partition("{")
                walk(rest[:-1] if rest.endswith("}") else rest, prefix + head)
            elif it:
                out.append(prefix + it)

    walk(s, "")
    return [re.sub(r"::\*$", "", p).strip(":") for p in out]


def imports(name, text, state):
    crate = _crate(name, state["crates"])
    if crate is None:
        return set()
    mods, names = state["mods"], state["names"]
    own = _module(name, crate)
    if own is None:
        # tests/, examples/ and benches/ beside Cargo.toml import the crate itself.
        return {mods[(crate, ())]} - {name} if (crate, ()) in mods else set()
    folder, _, leaf = name.rpartition("/")
    base = folder if leaf in _ROOTS else f"{folder}/{leaf[:-3]}"
    deps = set()
    for m in _MOD.findall(text):
        for cand in (f"{base}/{m}.rs", f"{base}/{m}/mod.rs"):
            if cand in names:
                deps.add(cand)
                break
    for stmt in _USE.findall(text):
        for path in _expand(stmt):
            segs = path.split("::")
            if segs[0] == "crate":
                cur, rest = (), segs[1:]
            elif segs[0] in ("self", "super"):
                cur, rest = own, segs
                while rest and rest[0] in ("self", "super"):
                    if rest[0] == "super":
                        cur = cur[:-1]
                    rest = rest[1:]
            else:
                continue
            for i in range(len(rest), -1, -1):
                f = mods.get((crate, cur + tuple(rest[:i])))
                if f:
                    deps.add(f)
                    break
    return deps - {name}
