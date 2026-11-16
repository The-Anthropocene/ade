# id: ade.config
# what: find the repo root (the main worktree, even from a lane), load ade.toml over the bundled template, match globs
# cmd: -
# reads: ade.toml, ade/template.toml
# writes: -
import fnmatch
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ade import host
from ade.telemetry import PLANS
from ade.refuse import refuse

KIT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).with_name("template.toml")


@dataclass
class Ctx:
    root: Path
    cfg: dict
    renumbered: dict = field(default_factory=dict)  # {old: new} card ids push.push moved when records met another machine's

    @property
    def state(self):
        return self.root / ".ade"


def merge(base, over):
    # Tables merge key by key; lists and scalars from ade.toml replace the template's.
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def toplevel(cwd):
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def main_worktree(cwd):
    # Lanes are worktrees elsewhere on disk; records and config belong to the main checkout.
    out = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel")
    if out is None:
        return None
    common, _, top = out.partition("\n")
    common = Path(common).resolve()
    return common.parent if common.name == ".git" else Path(top).resolve()


CHOICES = {("usage", "plan"): tuple(PLANS), ("usage", "unit"): ("dollars", "meter", "plan_dollars"),
           ("land", "strays"): ("off", "warn")}


def load_at(root, own=True):  # own=False: the template alone, for a broken ade.toml
    cfg = tomllib.loads(host.read(TEMPLATE))
    f = Path(root) / "ade.toml"
    if own and f.is_file():
        cfg = merge(cfg, tomllib.loads(host.read(f)))
    for kind in ("allow", "deny"):  # a rule set to false in ade.toml drops the template's rule, for every reader
        rules = cfg["permissions"][kind]
        cfg["permissions"][kind] = {k: v for k, v in rules.items() if v is not False}
    _flight(cfg, json.loads(os.environ.get("ADE_FLIGHT") or "{}"))
    from ade.providers import check_pass_env  # here, not at the top: providers imports install, which imports config
    check_pass_env(cfg)
    for (sec, key), ok in CHOICES.items():  # one table of the values a key takes; a typo refuses here, never guessed
        if cfg.get(sec, {}).get(key, ok[0]) not in ok:
            refuse("config.value", f"[{sec}] {key} = {cfg[sec][key]!r} is not a value", f"use one of: {', '.join(ok)}")
    return Ctx(Path(root).resolve(), cfg)


def _flight(cfg, fl):
    if fl.get("hold"):  # --hold-land CARD: land stops for a person's 'land' on this card, this run only
        cfg.setdefault("land", {})["hold"] = fl["hold"]
    # This run's --model ROLE=ENGINE:MODEL and --effort ROLE=LEVEL, laid over the loaded config; never written back.
    for kind in ("model", "effort"):
        for spec in fl.get(kind, []):
            role, _, rest = spec.partition("=")
            eng, _, val = rest.partition(":") if kind == "model" else ("", "", rest)
            if role not in cfg["roles"] or not val or (kind == "model" and not eng):
                refuse("flight.spec", f"--{kind} {spec!r} is not ROLE=" + ("ENGINE:MODEL" if kind == "model" else "LEVEL") + " for a role in ade.toml",
                       "e.g. --model builder=claude:haiku --effort builder=max")
            r = cfg["roles"][role]
            if kind == "model":
                r["provider"] = eng
            r.setdefault("models" if kind == "model" else "effort", {})[r["provider"]] = val


def load(cwd):
    root = main_worktree(cwd)
    if root is None or not (root / "ade.toml").is_file():
        refuse("config.missing", f"no ade.toml in the repository at {cwd}",
               "run `ade init` in the repository root (add --dry-run to preview)")
    return load_at(root)


def matches(path, globs):
    # fnmatch's * crosses '/', so 'src/*' covers the whole subtree; that is the intended reading.
    return any(g == path or fnmatch.fnmatchcase(path, g) for g in globs)


def rigour(paths, cfg):
    return "proof" if any(matches(p, cfg["rigour"]["proof_grade"]) for p in paths) else "orchestration"
