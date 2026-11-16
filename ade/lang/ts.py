# id: ade.lang.ts
# what: TypeScript/JavaScript reader: import, export-from, require and import() resolved with extensions, /index and tsconfig paths
# cmd: -
# reads: tsconfig.json or jsconfig.json at the repo root
# writes: -
import json
import posixpath
import re
from pathlib import Path

EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_SPEC = re.compile(r"""(?:\b(?:import|export)\s[^'";]*?\bfrom\s*|\bimport\s*|\b(?:require|import)\s*\(\s*)['"]([^'"\n]+)['"]""")
_JOIN = re.compile(r"""\brequire\s*\(\s*path\.(?:join|resolve)\s*\(\s*(__dirname|[A-Za-z_$][\w$]*)((?:\s*,\s*(?:'[^'\n]*'|"[^"\n]*"))+)\s*\)\s*\)""")
_ROOT = re.compile(r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*path\.(?:join|resolve)\s*\(\s*__dirname((?:\s*,\s*(?:'[^'\n]*'|"[^"\n]*"))*)\s*\)""")
_STR = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")
# ESM TypeScript writes the emitted name (`./x.js`) for a source file `x.ts`.
_EMITTED = {".js": (".ts", ".tsx"), ".mjs": (".mts",), ".cjs": (".cts",), ".jsx": (".tsx",)}


def _jsonc(text):
    # tsconfig allows comments and trailing commas; drop both outside strings, then parse as JSON.
    out, i, s = [], 0, None
    while i < len(text):
        c = text[i]
        if s:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1:i + 2])
                i += 1
            elif c == s:
                s = None
        elif c == '"':
            s = c
            out.append(c)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
            continue
        elif text.startswith("/*", i):
            i = text.find("*/", i + 2)
            i = len(text) if i < 0 else i + 2
            continue
        else:
            out.append(c)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def _options(root, rel, depth=0):
    """compilerOptions of one config with relative `extends` followed (the child wins). baseUrl comes back
    repo-relative, and so does pathsBase, which is where `paths` resolve from. Package extends are skipped."""
    p = Path(root) / rel
    try:
        cfg = _jsonc(p.read_text(encoding="utf-8-sig"))  # utf-8-sig: editors on Windows may write a BOM
    except (OSError, ValueError):
        return {}
    here = posixpath.dirname(rel)
    opts = {}
    ext = cfg.get("extends", [])
    for parent in [ext] if isinstance(ext, str) else ext:
        if parent.startswith(".") and depth < 8:
            target = posixpath.normpath(posixpath.join(here, parent))
            opts.update(_options(root, target if target.endswith(".json") else target + ".json", depth + 1))
    own = dict(cfg.get("compilerOptions", {}))
    if "baseUrl" in own:
        own["baseUrl"] = posixpath.normpath(posixpath.join(here, own["baseUrl"]))
    if "paths" in own:
        own["pathsBase"] = own.get("baseUrl", opts.get("baseUrl", posixpath.normpath(here or ".")))
    opts.update(own)
    return opts


def prepare(root, names):
    rel = next((f for f in ("tsconfig.json", "jsconfig.json") if (Path(root) / f).is_file()), None)
    opts = _options(root, rel) if rel else {}
    pbase = opts.get("pathsBase", ".")
    paths = [(k, [posixpath.normpath(posixpath.join(pbase, t)) for t in v]) for k, v in opts.get("paths", {}).items()]
    return {"root": Path(root), "files": set(names), "base": opts.get("baseUrl"), "paths": paths}


def _resolve(b, files):
    stem, ext = posixpath.splitext(b)
    cands = [b] + [b + e for e in EXTS] + [f"{b}/index{e}" for e in EXTS]
    cands += [stem + e for e in _EMITTED.get(ext, ())]
    return next((c for c in cands if c in files), None)


def _targets(spec, name, st):
    if spec.startswith("."):
        return [posixpath.normpath(posixpath.join(posixpath.dirname(name), spec))]
    out = []
    for pat, targets in st["paths"]:
        pre, star, post = pat.partition("*")
        if (star and spec.startswith(pre) and spec.endswith(post) and len(spec) >= len(pre) + len(post)) or spec == pat:
            mid = spec[len(pre):len(spec) - len(post)] if star else ""
            out += [t.replace("*", mid, 1) for t in targets]
    if st["base"] is not None:
        out.append(posixpath.normpath(posixpath.join(st["base"], spec)))
    return out  # anything else is a package (react-native): not an edge


def imports(name, text, st):
    """Code files it requires, plus any literal-required .json file (a data edge, which the graph files as a caller)."""
    deps = set()
    specs = _SPEC.findall(text)
    roots = {n: [a or b for a, b in _STR.findall(args)] for n, args in _ROOT.findall(text)}  # const ROOT = path.resolve(__dirname, "..")
    for base, args in _JOIN.findall(text):  # require(path.join(__dirname or ROOT, "a", "b.js")): every argument a literal
        if base != "__dirname" and base not in roots:
            continue
        parts = ([] if base == "__dirname" else roots[base]) + [a or b for a, b in _STR.findall(args)]
        if not any(p.startswith("/") for p in parts):
            specs.append("./" + "/".join(parts))
    for spec in specs:
        for t in _targets(spec, name, st):
            hit = _resolve(t, st["files"])
            if not hit and t.endswith(".json") and (st["root"] / t).is_file():
                hit = t
            if hit and hit != name:
                deps.add(hit)
                break
    return deps
