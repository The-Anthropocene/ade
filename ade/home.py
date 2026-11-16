# id: ade.home
# what: what a bare `ade` prints: a small logo, this folder's status line, and every verb with a few words on it
# cmd: ade
# reads: ade.toml and .ade/records.jsonl when run inside an ade repo
# writes: -
import os
import sys

from ade import TAGLINE, config, firstrun
from ade.refuse import Refusal

LOGO = r"""   __ _  __| | ___
  / _` |/ _` |/ _ \
 | (_| | (_| |  __/
  \__,_|\__,_|\___|"""

# Every verb once, in the order a newcomer meets them; a test fails when a verb is added and left out of this list.
# `hook` is left out on purpose: Claude Code calls it, a person never does.
GROUPS = [
    ("Start", [("init", "start a project here, or install into this repo"),
               ("link", "make a plain `ade` work in new shells"),
               ("config [word]", "find any ade.toml setting, its value and what it does"),
               ("parts", "list the kit's parts; `parts set x=off` turns one off"),
               ("setup", "a panel to switch parts on and off, with the diff first")]),
    ("Work", [("card add <title>", "a change to make, with its paths and done line"),
              ("loop <card>", "lane, build, review, test, land"),
              ("conduct", "loop every open card, waking a model only when needed"),
              ("land <card>", "merge a reviewed, tested lane"),
              ("push", "send trunk and records; fast-forward only")]),
    ("Look", [("cockpit", "every open card and its next command"),
              ("status", "the one line above"),
              ("watch <card>", "follow a lane live"),
              ("telemetry", "tokens, cost and week meters"),
              ("budget", "lines, hooks and refusals against their limits")]),
    ("Decide", [("decide", "rulings, and questions with a default and a deadline"),
                ("digest", "what landed, parked or defaulted today")]),
    ("Tools", [("handover --role <r>", "an agent's starting context"),
               ("q", "store long output, then page or query it"),
               ("session", "run a verb detached, list or stop it"),
               ("wait", "wait for a condition, with a bound"),
               ("graph", "regenerate the code graph"),
               ("rehearse", "full loops on throwaway repos, spending nothing"),
               ("uninstall", "remove what init wrote, keep your records")]),
]
HIDDEN = {"hook"}


def _paint(code, text):
    on = sys.stdout.isatty() and "NO_COLOR" not in os.environ and (os.name != "nt" or "WT_SESSION" in os.environ)
    return f"\033[{code}m{text}\033[0m" if on else text


def _next(ctx):
    from ade import records
    from ade.verbs import conduct, decide
    from ade.verbs.handover import next_command
    cards, journal = records.merged(ctx, "card"), records.read(ctx, "journal")
    open_cards = [c for c in cards.values() if c["status"] != "landed"]
    if not open_cards:
        return "\nnext: ade card add <title> --path <glob> --done <line>"
    for c in open_cards:
        if not conduct.waiting(ctx, c):
            return f"\nnext: {next_command(ctx, c, journal)} ({c['id']}: {c['title']})"
    qs = len(decide.queue(ctx)) + len(decide.waiting(ctx))
    return f"\nnext: ade decide ({qs} to decide)" if qs else ""


def show(cwd):
    greeting = []
    try:
        from ade.verbs import status
        ctx = config.load(cwd)
        here = status.line(ctx) + _next(ctx)
    except Refusal:
        here = "not set up here: `ade init` starts a project in an empty folder or installs into a repo"
        greeting = firstrun.welcome()
    print(_paint("36", LOGO) + "   " + _paint("2", TAGLINE))
    print("\n" + "".join(line + "\n" for line in greeting) + f"{here}\n\n" + _paint("1", "What can I do?"))
    for group, rows in GROUPS:
        print(_paint("2", f"  {group}"))
        for cmd, what in rows:
            print(f"    {_paint('36', ('ade ' + cmd).ljust(26))}{what}")
    print("\n" + _paint("2", "ade <verb> -h for one verb's options · ade -h for the full list"))
    return 0
