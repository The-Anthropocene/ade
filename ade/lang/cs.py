# id: ade.lang.cs
# what: C# reader: a using resolves to the .cs files declaring that namespace whose stem appears as a whole word in the text, or to the named type; the own namespace and its parents count the same way
# cmd: -
# reads: the .cs files' namespace lines
# writes: -
import posixpath
import re
from pathlib import Path

EXTS = (".cs",)
_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
_USING = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;", re.M)


def prepare(root, names):
    spaces = {}  # namespace -> {stem: file}
    of = {}  # file -> its namespaces
    for n in names:
        try:
            found = _NAMESPACE.findall((Path(root) / n).read_text(errors="replace"))
        except OSError:
            continue
        of[n] = found
        for ns in found:
            spaces.setdefault(ns, {}).setdefault(posixpath.splitext(posixpath.basename(n))[0], n)
    return {"spaces": spaces, "of": of}


def _mentioned(ns, text, state, name):
    return {f for stem, f in state["spaces"].get(ns, {}).items()
            if f != name and re.search(rf"(?<![\w$]){re.escape(stem)}(?![\w$])", text)}


def imports(name, text, state):
    deps = set()
    for m in _USING.finditer(text):
        path = m.group(1)
        if path in state["spaces"]:
            deps |= _mentioned(path, text, state, name)
        else:  # App.Util.Strings names the type directly
            ns, _, stem = path.rpartition(".")
            hit = state["spaces"].get(ns, {}).get(stem)
            if hit and hit != name:
                deps.add(hit)
    for ns in state["of"].get(name, []):
        parts = ns.split(".")
        for i in range(len(parts), 0, -1):
            deps |= _mentioned(".".join(parts[:i]), text, state, name)
    return deps
