# id: ade.lang.jvm
# what: Java and Kotlin reader: an import resolves to the file whose package line and stem match, under any source root; a wildcard or the own package reaches only files whose stem appears in the text
# cmd: -
# reads: the .java, .kt and .kts files' package lines
# writes: -
import posixpath
import re
from pathlib import Path

EXTS = (".java", ".kt", ".kts")
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(\.\*)?\s*(?:;|\s+as\s|$)", re.M)


def prepare(root, names):
    pkgs = {}  # package -> {stem: file}
    of = {}  # file -> package
    for n in names:
        try:
            m = _PACKAGE.search((Path(root) / n).read_text(errors="replace"))
        except OSError:
            continue
        pkg = m.group(1) if m else ""
        of[n] = pkg
        pkgs.setdefault(pkg, {}).setdefault(posixpath.splitext(posixpath.basename(n))[0], n)
    return {"pkgs": pkgs, "of": of}


def _named(path, pkgs):
    # Trailing segments may be a nested class or a static member: com.x.Foo.bar -> com.x.Foo.
    parts = path.split(".")
    for i in range(len(parts), 1, -1):
        hit = pkgs.get(".".join(parts[: i - 1]), {}).get(parts[i - 1])
        if hit:
            return hit
    return None


def _mentioned(pkg, text, state, name):
    return {f for stem, f in state["pkgs"].get(pkg, {}).items()
            if f != name and re.search(rf"(?<![\w$]){re.escape(stem)}(?![\w$])", text)}


def imports(name, text, state):
    deps = set()
    for m in _IMPORT.finditer(text):
        if m.group(2) and m.group(1) in state["pkgs"]:
            deps |= _mentioned(m.group(1), text, state, name)
        else:
            hit = _named(m.group(1), state["pkgs"])
            if hit and hit != name:
                deps.add(hit)
    deps |= _mentioned(state["of"].get(name, ""), text, state, name)
    return deps
