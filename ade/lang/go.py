# id: ade.lang.go
# what: Go reader: an import path under a go.mod module resolves to every non-test .go file in that folder; standard library and third-party paths are no edge
# cmd: -
# reads: go.mod beside or above the .go files
# writes: -
import posixpath
import re
from pathlib import Path

EXTS = (".go",)
_MODULE = re.compile(r"^\s*module\s+(?:\"([^\"]+)\"|(\S+))", re.M)
_STR = re.compile(r'"([^"\n]+)"|`([^`\n]+)`')
_IMPORT = re.compile(r"^\s*import\s*(?:\(([^)]*)\)|([^\n(]*))", re.M)


def _module(root, folder):
    try:
        m = _MODULE.search((Path(root) / folder / "go.mod").read_text(errors="replace"))
    except OSError:
        return None
    return (m.group(1) or m.group(2)) if m else None


def prepare(root, names):
    folders = {}  # folder -> its .go files
    for n in names:
        folders.setdefault(posixpath.dirname(n), []).append(n)
    mods = {}  # go.mod folder -> module path, for every folder holding or above a package
    for folder in folders:
        parts = folder.split("/") if folder else []
        for i in range(len(parts) + 1):
            up = "/".join(parts[:i])
            if up not in mods:
                mods[up] = _module(root, up)
    paths = {}  # import path -> package folder
    for folder in folders:
        parts = folder.split("/") if folder else []
        for i in range(len(parts), -1, -1):  # the deepest go.mod owns the folder
            up = "/".join(parts[:i])
            if mods.get(up):
                paths[posixpath.join(mods[up], *parts[i:])] = folder
                break
    return {"folders": folders, "paths": paths}


def imports(name, text, state):
    deps = set()
    for block in _IMPORT.finditer(text):
        for s in _STR.finditer(block.group(1) or block.group(2) or ""):
            folder = state["paths"].get(s.group(1) or s.group(2))
            if folder is not None:
                deps.update(f for f in state["folders"][folder] if not f.endswith("_test.go") and f != name)
    # One folder is one package: a test file in it sees the package's other files.
    if name.endswith("_test.go"):
        deps.update(f for f in state["folders"][posixpath.dirname(name)] if not f.endswith("_test.go") and f != name)
    return deps
