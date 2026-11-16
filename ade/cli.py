# id: ade.cli
# what: parse argv and dispatch to a verb discovered from ade/verbs (module name = verb, header `what` = help)
# cmd: ade <verb> [args]
# reads: ade/verbs/*.py
# writes: -
import argparse
import difflib
import importlib
import os
import pkgutil
import sys
from importlib import metadata

import ade.verbs
from ade import TAGLINE, config, host, records
from ade.graph import header
from ade.refuse import Refusal


def verbs():
    return {m.name.replace("_", "-"): mod for m in pkgutil.iter_modules(ade.verbs.__path__)
            if hasattr(mod := importlib.import_module(f"ade.verbs.{m.name}"), "run")}


def _version():
    # install.kit(): the version plus the commit when the kit is a checkout; an installed release has no pyproject, so its metadata names it.
    from ade import install
    text = install.kit()
    if text.startswith("unknown"):
        try:
            return metadata.version("atlas-ade") + text[len("unknown"):]
        except metadata.PackageNotFoundError:
            return "(from a clone, not installed)"
    return text


def parser():
    """The whole command line, built from each verb's own add(); the conductor's route is checked against it too."""
    p = argparse.ArgumentParser(prog="ade", description=TAGLINE + ".",
                            epilog="Settings: ade.toml; find one with `ade config WORD`. ade.toml is protected: you may run `ade parts set` (and `ade setup`); name any other key to the person, who changes it and reruns `ade init`.")
    p.add_argument("--version", action="version", version=f"ade {_version()}")
    sub = p.add_subparsers(dest="verb", required=True, metavar="<verb>")
    found = verbs()
    for name, mod in sorted(found.items()):
        what = (header(host.read(mod.__file__)) or {}).get("what", "")
        sp = sub.add_parser(name, help=what, description=what)
        if hasattr(mod, "add"):
            mod.add(sp)
    return p, found


def main(argv=None, cwd=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # a console that cannot hold a character gets '?', not a traceback
    cwd = cwd or os.getcwd()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from ade import home
        return home.show(cwd)
    p, found = parser()
    first = argv[0]
    if not first.startswith("-") and first not in found:
        hint = f"; did you mean {near[0]}?" if (near := difflib.get_close_matches(first, found, n=1)) else ""
        print(f"ade: no verb {first}; run ade alone for the verb list{hint}", file=sys.stderr)
        return 2
    args = p.parse_args(argv)
    try:
        return found[args.verb].run(args, cwd) or 0
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # the exit flush must not raise again
        return 141  # the shell's code for a closed pipe
    except Refusal as r:
        print(r.render(), file=sys.stderr)
        try:
            records.fired(config.load(cwd), r, getattr(args, "card", None))
        except Refusal:
            pass  # not an ade repo, so there are no records to count it in
        return 2
