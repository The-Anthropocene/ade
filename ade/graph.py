# id: ade.graph
# what: derive the graph from five-line headers (and the data files, globs and folders their reads and writes name), each language reader's imports, test globs and the docs that name a file
# cmd: -
# reads: tracked and untracked-not-ignored source and markdown files
# writes: -
import ast
import json
import re
import shlex
import tomllib
from stat import S_ISREG
from pathlib import Path, PurePosixPath

from ade import host, lang
from ade.config import matches
from ade.gitx import git

KEYS = ("id", "what", "cmd", "reads", "writes")
_LINE = re.compile(r"(?:#|//)\s*(id|what|cmd|reads|writes):\s*(.*)")
_PATHISH = re.compile(r"[A-Za-z0-9_./@-]+")


def paths(h, *keys):
    """The paths a header's reads/writes name: comma-separated, each maybe backquoted, spaces and '+' kept, '-' dropped."""
    return [t for k in keys for t in _entries(h, k) if not t.startswith("run:")]


def _entries(h, k):
    return [t for t in (x.strip().strip("`").strip() for x in (h.get(k) or "").split(",")) if t and t != "-"]


def _found(text):
    found = {}
    for line in text.splitlines()[:8]:
        m = _LINE.match(line)
        if m:
            found[m[1]] = m[2].strip()
    if found and "writes" not in found and len(found) == len(KEYS) - 1:
        found["writes"] = "-"  # a reads-only header still counts: it writes nothing
    return found


def header(text):
    found = _found(text)
    return found if len(found) == len(KEYS) else None


def lacks(text):
    """The keys a partial header misses (none when it is whole or absent)."""
    found = _found(text)
    return [k for k in KEYS if k not in found] if found else []


def _listed(root, names=None):
    # Untracked files count, so a lane's new module is in the graph before it is committed.
    # build() lists once and passes the list on; a step called alone lists for itself.
    return git(root, "ls-files", "-co", "--exclude-standard").splitlines() if names is None else names


def _sources(root, cfg, readers, names=None):
    names = _listed(root, names)
    by_ext = {e: lg for lg, r in readers.items() for e in r.EXTS}
    for name in sorted(set(names)):
        p = root / name
        if matches(name, cfg["graph"]["exclude"]):
            continue
        lg = by_ext.get(PurePosixPath(name).suffix)
        try:
            if not S_ISREG(p.stat().st_mode):
                continue
            if lg is None and "." not in PurePosixPath(name).name:
                with open(p, "rb") as f:
                    first = f.readline()
                lg = "py" if first.startswith(b"#!") and b"python" in first else None
            if lg or name.endswith(".md"):
                yield name, host.read(p, errors="replace"), lg
        except FileNotFoundError:  # tracked but deleted in the worktree (not yet staged): it leaves the graph
            continue
        except OSError:  # a sandbox or secrets guard denies the read: build() carries this file's entry from the old graph
            yield name, None, lg


def _docs(texts, files):
    # Prose that names a code file by its repo path; staleness lives where prose describes code.
    out = {}
    for name, text in texts.items():
        for tok in {t.removeprefix("./").rstrip(".") for t in _PATHISH.findall(text)}:  # one entry per (file, doc)
            if tok in files:
                out.setdefault(tok, []).append(name)
    return {k: sorted(v) for k, v in sorted(out.items())}


_SEG = re.compile(r"&&|\|\||[;|`]|\$\(")


def ade_calls(text):
    """(line number, argv after `ade`) for each ade command a script runs: a shell segment whose command is ade,
    bin/ade or `python -m ade`, as a shell line, a workflow `run:` or a listed step. Comments and prose do not count."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if line.startswith(("#", "//", "REM ")):
            continue
        for seg in _SEG.split(line):
            seg = re.sub(r"^\s*(?:-\s+)?(?:run:\s*|\$\s+)?", "", seg).strip().strip("'\"")
            try:
                toks = shlex.split(seg, comments=True)
            except ValueError:
                continue
            if toks[:3] and PurePosixPath(toks[0].replace("\\", "/")).stem in ("python", "python3", "py") \
                    and toks[1:3] == ["-m", "ade"]:
                toks = toks[2:]
            if len(toks) > 1 and PurePosixPath(toks[0].replace("\\", "/")).stem == "ade" and re.match(r"[a-z]", toks[1]):
                out.append((n, toks[1:]))
    return out


ROUTE = "ade/route.toml"


def _binds(root, cfg, names=None):
    # Scripts bound to the verbs they run, and the conductor's route to the verbs and stages it names
    # (ARCHITECTURE.md 12.6): the briefing reads these, and land checks each one still parses.
    names = _listed(root, names)
    out = {}
    for name in sorted(set(names)):
        p = root / name
        script = matches(name, cfg["graph"]["scripts"]) and not matches(name, cfg["graph"]["exclude"])
        if name != ROUTE and not script:
            continue
        try:
            if not S_ISREG(p.stat().st_mode):
                continue
            if name == ROUTE:
                rows = tomllib.loads(host.read(p)).get("stop", [])
                got = {f"ade {shlex.split(r['run'])[1]}" for r in rows if r.get("run")}
                got |= {f"stage:{r['stage']}" for r in rows if r.get("stage", "*") != "*"}
            else:
                got = {f"ade {argv[0]}" for _, argv in ade_calls(host.read(p, errors="replace"))}
        except OSError:  # deleted, or denied by a sandbox: binds only what can be read
            continue
        if got:
            out[name] = sorted(got)
    return out


def build(root, cfg):
    root = Path(root)
    readers = lang.readers()
    tracked = _listed(root)
    found = list(_sources(root, cfg, readers, tracked))
    unreadable = sorted(n for n, t, _ in found if t is None)
    old = {}
    if unreadable:  # only then is the committed graph consulted; a stale or conflicted one carries nothing
        try:
            old = load(root, cfg) or {}
        except ValueError:
            old = {}
    # An unreadable file keeps its committed files row (so it stays code) and its doc edges.
    code = {n: (t, lg) for n, t, lg in found if lg and t is not None}
    code.update({n: (None, old["files"][n]["lang"]) for n in unreadable if n in old.get("files", {})})
    states = {lg: r.prepare(root, [n for n, (t, x) in code.items() if x == lg and t is not None]) for lg, r in readers.items()}
    files, data = {}, {}
    for name, (text, lg) in code.items():
        if text is None:
            files[name] = old["files"][name]
            data[name] = sorted(h for h, cs in old.get("callers", {}).items() if name in cs and h not in code)
            continue
        got = readers[lg].imports(name, text, states[lg])
        data[name] = sorted(x for x in got if x not in code)  # a data file a reader saw required, e.g. x.json
        files[name] = {"header": header(text), "imports": sorted(got & code.keys()),
                       "test": matches(name, cfg["graph"]["tests"]), "lang": lg}
        if lacks(text):
            files[name]["lacks"] = lacks(text)
    callers = {n: sorted(m for m, f in files.items() if n in f["imports"]) for n in files}
    # A data or config file no import reaches is still read by the code whose header names it (reads or writes):
    # those files are its callers, so two cards meeting only there are kept apart and its readers' tests run.
    names = {n for n in tracked
             if n not in files and n != cfg["graph"]["out"]  # the graph itself is derived, rewritten by every lane
             and not matches(n, cfg["graph"]["exclude"] + cfg["rigour"]["no_review"])}
    for n, f in files.items():
        for hit in data[n]:
            if hit in names:
                callers.setdefault(hit, []).append(n)
        # An entry that is not itself a tracked name or a glob is prose ("ade.toml [guards]"): fall back to its path-like words.
        ents = paths(f["header"] or {}, "reads", "writes")
        for tok in set(t for e in ents for t in ([e] if e in names or "*" in e or "?" in e else [e] + re.findall(r"[A-Za-z0-9_./@*?-]+", e))):
            # A token with * or ? is a glob over the tracked non-code names; any other must be exactly one.
            for hit in (sorted(x for x in names if matches(x, [tok])) if "*" in tok or "?" in tok else [tok] * (tok in names)):
                callers.setdefault(hit, []).append(n)
    # reads: run:PATH (a path or glob) makes the declaring file a caller of each matching file: it runs it.
    for n, f in files.items():
        for e in _entries(f["header"] or {}, "reads"):
            if e.startswith("run:"):
                for hit in sorted(x for x in tracked if x != n and matches(x, [e[4:].strip()])):
                    if n not in callers.get(hit, []):
                        callers.setdefault(hit, []).append(n)
    reach = {}
    for t in (n for n, f in files.items() if f["test"]):
        seen, todo = set(), list(files[t]["imports"])
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.add(n)
                todo += files[n]["imports"]
        for n in seen:
            reach.setdefault(n, set()).add(t)
    for n in names & callers.keys():
        reach[n] = set().union(*(reach.get(r, set()) for r in callers[n]))
    # A test whose header names a tracked folder or file (prose included) reaches everything at or under it, for test
    # selection only: callers, and so review and clash, are untouched.
    for t in (n for n, f in files.items() if f["test"]):
        for tok in set(_PATHISH.findall(" ".join((files[t]["header"] or {}).get(k, "") for k in ("reads", "writes")))):
            under = tok.rstrip("/") + "/"
            for n in tracked:
                if n != cfg["graph"]["out"] and n != t and (n == tok or n.startswith(under)):
                    reach.setdefault(n, set()).add(t)
    # A Python test's string literal that is exactly a tracked file or folder (a subprocess argv, a fixture path)
    # reaches it the same way; other readers are untouched.
    for t in (n for n, f in files.items() if f["test"] and f["lang"] == "py"):
        try:
            lits = {x.value.rstrip("/") for x in ast.walk(ast.parse(code[t][0])) if isinstance(x, ast.Constant)
                    and isinstance(x.value, str) and x.value.strip("/")}
        except (SyntaxError, TypeError):
            continue
        for n in tracked:
            parts = n.split("/")
            if n != cfg["graph"]["out"] and n != t and lits & {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}:
                reach.setdefault(n, set()).add(t)
    docs = _docs({n: t for n, t, lg in found if not lg and t is not None and not matches(n, cfg["graph"]["history"])}, files)
    for tok, mds in old.get("docs", {}).items():
        if tok in files and set(mds) & set(unreadable):
            docs[tok] = sorted(set(docs.get(tok, [])) | (set(mds) & set(unreadable)))
    return {"generated_by": "ade graph", "files": files, "callers": {k: sorted(set(v)) for k, v in sorted(callers.items()) if v},
            "tests": {k: sorted(v) for k, v in sorted(reach.items()) if v},
            "docs": dict(sorted(docs.items())), "binds": _binds(root, cfg, tracked), "unreadable": unreadable}


def render(g):
    return json.dumps({k: v for k, v in g.items() if k != "unreadable"}, indent=1, sort_keys=True) + "\n"


def load(root, cfg):
    p = Path(root) / cfg["graph"]["out"]
    return json.loads(host.read(p)) if p.is_file() else None


def comoves(g, rel):
    """Co-move edges X -> Y via G from code headers with 'reads: X' and 'writes: Y' (paths or globs, not '-'):
    for a file matching X, the (Y, G) pairs that are generated from it."""
    def toks(h, k):
        return paths(h, k)
    return [(y, n) for n, f in sorted(g["files"].items()) if f.get("header") and n != rel
            for x in toks(f["header"], "reads") if matches(rel, [x])
            for y in toks(f["header"], "writes")]


def writers(g, rel):
    """The files whose header's writes: glob matches rel: the tools that generate it."""
    return [n for n, f in sorted(g["files"].items()) if f.get("header") and n != rel
            and any(matches(rel, [w]) for w in paths(f["header"], "writes"))]


def rewriters(g, rel):
    """The tools whose header names rel (or a glob or folder matching it) in both reads and writes: they rewrite it in place."""
    return [n for n, f in sorted(g["files"].items()) if f.get("header") and n != rel
            and any(matches(rel, [x]) for x in paths(f["header"], "reads"))
            and any(matches(rel, [w]) for w in paths(f["header"], "writes"))]


def reach(g, globs, prose=()):
    """What a card's paths can disturb: the files they name, those files' callers and the tests that reach them.
    A code path the graph has no edges for (a language without a reader, or a file not written yet) stands for its
    whole folder instead, written "folder/", so two cards meeting only there are kept apart rather than guessed
    apart. Prose (the globs in `prose`) calls nothing, so it stands only for itself."""
    out = set()
    for pattern in globs:
        named = [n for n in {*g["files"], *g["callers"]} if matches(n, [pattern])]
        for n in named:
            out.add(n)
            out.update(g["callers"].get(n, []))
            out.update(g["tests"].get(n, []))
        if matches(pattern, prose):
            out.add(pattern)
        elif not any(g["files"].get(n, {}).get("imports") or n in g["callers"] for n in named):
            out.add(pattern.split("*")[0].rpartition("/")[0] + "/")
    return out


def clash(a, b):
    # Shared files clash; a folder clashes with anything inside it, and "/" (the repo root) with everything.
    def inside(x, d):
        return d == "/" or x.startswith(d)
    return bool(a & b) or any(inside(x, d) for d in a if d.endswith("/") for x in b) or \
        any(inside(x, d) for d in b if d.endswith("/") for x in a)


def select_tests(g, paths, cfg):
    """Tests reaching the touched paths; [] means none are needed, None means the whole suite."""
    if any(matches(p, cfg["rigour"]["proof_grade"]) for p in paths):
        return None  # proof-grade: the whole suite, never the graph's selection (CHARTER section 4)
    chosen, fallback = set(), False
    for p in paths:
        f = g["files"].get(p)
        if matches(p, cfg["rigour"]["no_review"]):
            chosen.update(g["tests"].get(p, []))  # prose reaches no code, but a test may read it
            continue
        # A test-tree helper (tests/helpers.py) is imported, never run: its importers stand in for it, as running
        # it alone finds no tests (exit 5). A real test file is imported by nothing, so it still selects itself.
        if f and f["test"] and not (g["tests"].get(p) and any(p in o["imports"] for o in g["files"].values())):
            chosen.add(p)
        elif g["tests"].get(p):
            chosen.update(g["tests"][p])
        else:
            fallback = True  # unknown or untested path: the small critical set stands in
    if fallback:
        if not cfg["tests"]["critical"]:
            return None  # no critical set configured yet; ade.toml [tests].critical narrows this
        chosen.update(cfg["tests"]["critical"])
    return sorted(chosen)
