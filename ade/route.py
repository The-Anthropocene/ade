# id: ade.route
# what: the conductor's route table: load it, pick the row for a stop, and check it and every script against the verbs
# cmd: -
# reads: ade/route.toml, ade/stages/*.py and ade/*.py source, the verbs' own parsers, graph binds
# writes: -
import ast
import contextlib
import io
import shlex
import tomllib

from ade import cli, graph, host, stages, wait
from ade.config import KIT

FILE = KIT / graph.ROUTE
ACTIONS = ("retry", "wait", "wake", "park")
OUTCOMES = ("wait", "needs-judgement")  # advance and done never reach the route


def load():
    return tomllib.loads(host.read(FILE))


def pick(rows, stage, outcome, code=None):
    """The most specific row for a stop: stage and code, then code, then stage, then "*"."""
    def fits(r):
        return r["outcome"] == outcome and r.get("stage", "*") in (stage, "*") and r.get("code") in (None, code)
    hits = [r for r in rows if fits(r)]
    return max(hits, key=lambda r: (r.get("code") is not None, r.get("stage", "*") != "*"), default=None)


def _calls(name):
    # Constant first arguments of every call to `name` in the kit's source: Result("wait", ...) or refuse("x.y", ...).
    found = {}
    for p in sorted((KIT / "ade").rglob("*.py")):
        for node in ast.walk(ast.parse(host.read(p))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == name
                    and node.args and isinstance(node.args[0], ast.Constant)):
                found.setdefault(p.stem, set()).add(node.args[0].value)
    return found


def returns():
    """(stage, outcome) for every stop the driver can make: each stage's own Results, plus a refusal inside any stage,
    which the driver turns into needs-judgement."""
    made = _calls("Result")
    return sorted({(s, o) for s in stages.STAGES for o in made.get(s, set()) if o in OUTCOMES}
                  | {(s, "needs-judgement") for s in stages.STAGES})


def parses(argv):
    """None when `ade <argv>` parses with the verbs' own parsers, else argparse's complaint."""
    p, _ = cli.route_parser()
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            p.parse_args(argv)
    except SystemExit as e:
        if e.code:
            return (err.getvalue().strip().splitlines() or ["does not parse"])[-1]
    return None


def check(ctx, root):
    """Every way the route or a bound script has drifted from the loop, as (where, what) pairs; [] means bound."""
    bad = []
    route = load()
    rows = route.get("stop", [])
    codes = set().union(*_calls("refuse").values())
    runs = [("[start]", route.get("start", {}).get("run"))] + [(f"stop {i + 1}", r.get("run")) for i, r in enumerate(rows)]
    for where, line in runs:
        if line:
            argv = shlex.split(line.replace("{card}", "c1"))[1:]
            why = parses(argv)
            if why:
                bad.append((f"{graph.ROUTE} {where}", f"`{line}`: {why}"))
    for i, r in enumerate(rows):
        where = f"{graph.ROUTE} stop {i + 1}"
        if r.get("stage", "*") not in (*stages.STAGES, "*"):
            bad.append((where, f"no stage {r['stage']!r}; stages are {', '.join(stages.STAGES)}"))
        if r.get("outcome") not in OUTCOMES:
            bad.append((where, f"outcome {r.get('outcome')!r} never reaches the route; use {' or '.join(OUTCOMES)}"))
        if r.get("then") not in ACTIONS:
            bad.append((where, f"then {r.get('then')!r} is not one of {', '.join(ACTIONS)}"))
        if r.get("code") and r["code"] not in codes:
            bad.append((where, f"no refusal code {r['code']!r} in the kit's source"))
        if r.get("then") == "wake" and r.get("role") not in ctx.cfg["roles"]:
            bad.append((where, f"wakes role {r.get('role')!r}, which is not in [roles]"))
        # A route row has no argument to give its wait, so only the conditions that need none can be routed.
        routable = [w for w in ("clean", "time") if w in wait.CONDITIONS]
        if r.get("then") == "wait" and r.get("wait") not in routable:
            bad.append((where, f"waits on {r.get('wait')!r}; a route can wait on {' or '.join(routable)}"))
        if r.get("then") in ("retry", "wait", "wake") and not r.get("run"):
            bad.append((where, f"{r['then']} needs a run line"))
    for stage, outcome in returns():
        if pick(rows, stage, outcome) is None:
            bad.append((graph.ROUTE, f"nothing routes {stage}/{outcome}, which the driver can return"))
    for name in (graph.load(root, ctx.cfg) or {}).get("binds", {}):
        if name == graph.ROUTE:
            continue
        for n, argv in graph.ade_calls(host.read(root / name, errors="replace")):
            why = parses([a if not a.startswith(("$", "%")) else "x" for a in argv])
            if why:
                bad.append((f"{name}:{n}", f"`ade {shlex.join(argv)}`: {why}"))
    return bad
