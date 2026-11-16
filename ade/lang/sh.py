# id: ade.lang.sh
# what: shell reader: a script's command, or the first file argument of sh, bash, source, ., node or python, that names a file under the repo root or the script's folder is an edge
# cmd: -
# reads: repo files named by .sh/.bash scripts
# writes: -
import posixpath
import re
from pathlib import Path

EXTS = (".sh", ".bash")
_RUNNERS = {"sh", "bash", "source", ".", "node", "python", "python3"}


def prepare(root, names):
    return Path(root)


def imports(name, text, state):
    deps = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for part in re.split(r"[;&|]+", line):
            words = [w.strip("\"'") for w in part.split()]
            if not words:
                continue
            cmd = posixpath.basename(words[0])
            if cmd == "env":
                words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
                cmd = posixpath.basename(words[0]) if words else ""
            if cmd in _RUNNERS:
                words = [w for w in words[1:] if not w.startswith("-")][:1]
            else:
                words = words[:1]
            for w in words:
                if w.startswith("/") or re.match(r"[A-Za-z]:", w):
                    continue
                for base in ("", posixpath.dirname(name)):
                    p = posixpath.normpath(posixpath.join(base, w))
                    if p.split("/")[0] != ".." and p != name and (state / p).is_file():
                        deps.add(p)
                        break
    return deps
