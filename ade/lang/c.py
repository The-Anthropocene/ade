# id: ade.lang.c
# what: C and C++ reader: "x.h" resolves from the including file's folder, the repo root, then include/ folders; <x.h> only through include/ folders; a header also imports its same-stem source file
# cmd: -
# reads: -
# writes: -
import posixpath
import re

EXTS = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx")
_SOURCES = (".c", ".cc", ".cpp", ".cxx")
_HEADERS = (".h", ".hpp", ".hh", ".hxx")
_INCLUDE = re.compile(r'^\s*#\s*include\s*(?:"([^"\n]+)"|<([^>\n]+)>)', re.M)


def prepare(root, names):
    files = {n for n in names if n.endswith(EXTS)}
    incs = set()
    for n in files:
        parts = n.split("/")[:-1]
        incs.update("/".join(parts[: i + 1]) for i, p in enumerate(parts) if p == "include")
    return {"files": files, "incs": sorted(incs)}


def _find(base, rel, files):
    p = posixpath.normpath(posixpath.join(base, rel)) if base else posixpath.normpath(rel)
    return p if p in files else None


def imports(name, text, state):
    files, deps = state["files"], set()
    here = posixpath.dirname(name)
    for m in _INCLUDE.finditer(text):
        quoted, angled = m.groups()
        bases = [here, "", *state["incs"]] if quoted else state["incs"]
        for b in bases:
            hit = _find(b, quoted or angled, files)
            if hit:
                deps.add(hit)
                break
    stem, ext = posixpath.splitext(name)
    if ext in _HEADERS:
        deps.update(f for e in _SOURCES if (f := stem + e) in files)
    deps.discard(name)
    return deps
