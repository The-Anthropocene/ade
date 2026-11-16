# id: ade.budget
# what: measure core lines (total and per component), hooks, refusal codes and test functions against the budgets in ade.toml
# cmd: -
# reads: source files, ade/ package source
# writes: -
import ast
from collections import Counter

from ade import graph, host, install
from ade.config import KIT, matches
from ade.gitx import git


def text_of(node):
    """A string literal as is, an f-string with each {expr} shown as <expr>, anything else as <set at run time>."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else f"<{ast.unparse(v.value)}>" for v in node.values)
    return "<set at run time>"


def refusals():
    """One entry per refuse() call whose code is a literal: code, cause, cure and file:line."""
    out = []
    for p in sorted((KIT / "ade").rglob("*.py")):
        for node in ast.walk(ast.parse(host.read(p))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "refuse"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                rest = [text_of(a) for a in node.args[1:3]] + ["<set at run time>"] * 2
                out.append({"code": node.args[0].value, "cause": rest[0], "cure": rest[1],
                            "at": f"{p.relative_to(KIT)}:{node.lineno}"})
    return out


def refusal_codes():
    return sorted({r["code"] for r in refusals()})


def test_count(root):
    # Test functions under tests/, counted from the source so a skipped or renamed file still shows.
    # Only files git tracks count, so a stray untracked .py cannot move the row; outside a git repo, every file does.
    r = git(root, "ls-files", "--", "tests", check=False)
    files = ([root / n for n in r.stdout.splitlines() if n.endswith(".py")] if r.returncode == 0
             else (root / "tests").rglob("*.py"))
    return sum(isinstance(n, ast.FunctionDef) and n.name.startswith("test")
               for p in files for n in ast.walk(ast.parse(host.read(p))))


def is_core(ctx, name):
    b = ctx.cfg["budgets"]
    return matches(name, b["core"]) and not matches(name, b["core_exclude"]) and name != ctx.cfg["graph"]["out"]


def off(ctx):
    return not ctx.cfg["budgets"]["core"]


def core_files(ctx):
    names = git(ctx.root, "ls-files", "-co", "--exclude-standard").splitlines()
    return [n for n in names if is_core(ctx, n) and (ctx.root / n).is_file()]


def component(ctx, name):
    # Each core file belongs to exactly one component; none or several is a hole in the budget, reported as a row.
    comps = ctx.cfg["budgets"]["components"]
    if not comps:
        return "core"
    hits = [k for k, v in comps.items() if matches(name, v["globs"])]
    return hits[0] if len(hits) == 1 else "overlapping" if hits else "unassigned"


class Rows(list):
    """The budget rows, plus a note (core bytes) that render prints under them; never a row, never a cap."""
    note = ""


def measure(ctx):
    b = ctx.cfg["budgets"]
    per = Counter()
    size = 0
    for n in core_files(ctx):
        per[component(ctx, n)] += len(host.read(ctx.root / n, errors="replace").splitlines())
        size += (ctx.root / n).stat().st_size
    rows = Rows([("core lines", sum(per.values()), b["core_lines"])])
    rows.note = f"core bytes {size / 1000:.0f} KB (note only, no cap)"
    if b["components"]:
        # The components' ceilings add up to the cap that really binds; printed beside the headline so it shows.
        rows += [("component ceilings", sum(v["lines"] for v in b["components"].values()), b["core_lines"])]
        rows += [(k, per[k], v["lines"]) for k, v in b["components"].items()]
        rows += [(k, per[k], 0) for k in ("unassigned", "overlapping") if per[k]]
    # Hooks and refusal codes are the kit's own budgets; a user's project shows only its lines.
    if not ((ctx.root / "ade" / "cli.py").is_file() and (ctx.root / graph.ROUTE).is_file()):
        return rows
    # Counted from what init emits, not the file: settings.local.json is gitignored, so CI never has one.
    s =install.local_settings({})
    rows += [("hooks", install.hook_count(s), b["hooks"]),
                   ("refusal codes", len(refusal_codes()), b["refusal_codes"]),
                   ("test functions", test_count(ctx.root), b["tests"])]
    return rows


def net_by_component(ctx, rev_range):
    # Net core lines a change adds per component; files outside the core budget are summed as "not core".
    net = Counter()
    for line in git(ctx.root, "diff", "--numstat", "--no-renames", rev_range).splitlines():
        add, rem, name = line.split("\t", 2)
        if add != "-":
            net[component(ctx, name) if is_core(ctx, name) else "not core"] += int(add) - int(rem)
    return dict(sorted(net.items()))


HARD = ("core lines", "hooks", "refusal codes", "test functions")  # the one rule: only these rows stop; the rest are notes


def over(rows):
    # The rows over their limit, split into (hard, notes); the verb and land both read this, so they never disagree.
    bad = [r for r in rows if r[1] > r[2]]
    return [r for r in bad if r[0] in HARD], [r for r in bad if r[0] not in HARD]


def render(rows):
    w = max(14, *(len(name) + 1 for name, _, _ in rows))
    return "\n".join([f"{name:<{w}} {val:>6} / {lim:<6} {'ok' if val <= lim else 'OVER' if name in HARD else 'over (note)'}" for name, val, lim in rows] + ([rows.note] if getattr(rows, "note", "") else []))
