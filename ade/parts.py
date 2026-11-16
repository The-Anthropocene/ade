# id: ade.parts
# what: the parts catalogue (ade/parts.toml) and the [parts] table in ade.toml: which parts are off, what turning one off warns, the ade.toml edit
# cmd: -
# reads: ade/parts.toml, ade.toml [parts]
# writes: -
import re
import tomllib
from pathlib import Path

CATALOGUE = Path(__file__).with_name("parts.toml")


def catalogue():
    return tomllib.loads(CATALOGUE.read_text(encoding="utf-8"))


def off(cfg):
    return set(cfg["parts"]["off"])


def files_off(cfg):
    """Generated files that init skips because their part is off."""
    cat = catalogue()
    return {f for p in off(cfg) if p in cat for f in cat[p]["files"]}


def warnings(cat, pid, now_off, used=True):
    """What turning `pid` off costs, given the parts then off: one line each, never a refusal."""
    out = [f"{q} needs {pid}, so {q} will not work" for q, c in cat.items() if pid in c["needs"] and q not in now_off]
    out += [f"{q} leans on {pid}, so {q} works with less" for q, c in cat.items() if used and pid in c["leans-on"] and q not in now_off]
    out += [f"{pid} needs {n}, which is off" for n in cat[pid]["needs"] if n in now_off]
    return out


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(off_ids, why):
    block = ["[parts]", "off = [" + ", ".join(_q(p) for p in sorted(off_ids)) + "]", "", "[parts.override]"]
    return "\n".join(block + [f"{p} = {_q(r)}" for p, r in sorted(why.items())]) + "\n"


def rewrite(text, off_ids, why):
    """ade.toml text with its [parts] tables replaced in place; every other line, comments and order included, untouched."""
    lines = text.splitlines()
    hdr = lambda l: re.match(r"\[parts(\.override)?\]\s*(#.*)?$", l)
    if not any(hdr(l) and l.startswith("[parts]") for l in lines):
        keep, skipping = [], False
        for line in lines:
            if line.startswith("["):
                skipping = bool(hdr(line))
            if not skipping:
                keep.append(line)
        return "\n".join(keep).rstrip() + "\n\n" + render(off_ids, why)
    body = {"[parts]": ["off = [" + ", ".join(_q(p) for p in sorted(off_ids)) + "]"],
            "[parts.override]": [f"{p} = {_q(r)}" for p, r in sorted(why.items())]}
    has_override = any(l.startswith("[parts.override]") for l in lines)
    out, tail, cur, done = [], [], None, True

    def close():
        if cur and not done:
            out.extend(body[cur])
        if cur == "[parts]" and not has_override:
            out.extend(["", "[parts.override]"] + body["[parts.override]"])
        out.extend(tail)
        tail.clear()

    for line in lines:
        if line.startswith("["):
            close()
            m = hdr(line)
            cur, done = (line.split("]")[0] + "]" if m else None), False
            out.append(line)
        elif cur and (not line.strip() or line.lstrip().startswith("#")):
            tail.append(line)
        elif cur:
            if not done:
                out.extend(tail + body[cur])
                tail.clear()
                done = True
        else:
            out.append(line)
    close()
    return "\n".join(out).rstrip() + "\n"
