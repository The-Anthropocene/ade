# id: ade.verbs.budget
# what: print core lines, hooks, refusal codes and tests against their budgets; fails when a hard row is over (component rows are notes); --refusals counts firings
# cmd: ade budget [--refusals] [--tests]
# reads: source files, .claude/settings.json, .ade/records.jsonl (refusal, journal, test_red and outcome rows)
# writes: -
from ade import budget, config, records
from ade.refuse import refuse


def add(p):
    p.add_argument("--refusals", action="store_true", help="each refusal code: times fired, and on cards that landed after")
    p.add_argument("--tests", action="store_true", help="tests red on two or more landed cards whose diffs never touched them")


def untouched(ctx):
    """Tests red on two or more cards that landed where the diff did not touch the test's subject: they guard
    nothing that broke, so they are up for replacement (the two-strike rule)."""
    landed = {o["card"] for o in records.read(ctx, "outcome")}
    cards = {}
    for r in records.read(ctx, "test_red"):
        if not r["touched"] and r["card"] in landed:
            cards.setdefault(r["test"], set()).add(r["card"])
    return "\n".join(f"{t}: red on {len(c)} landed cards that did not touch it ({', '.join(sorted(c))}); replace or retire it"
                     for t, c in sorted(cards.items()) if len(c) >= 2) or "no test is red twice on untouched cards"


def fired(ctx):
    """Every code in the source with how often it fired and how often its card landed later anyway. A code that
    never fires, or keeps firing on work that is then accepted, is a candidate to retire before adding another."""
    landed = {}
    for o in records.read(ctx, "outcome"):
        landed[o["card"]] = max(landed.get(o["card"], ""), o["at"])
    counts = {code: [0, 0] for code in budget.refusal_codes()}
    cards = {}
    parks = [dict(r, code=f"park: {r['stage']}: {(r.get('note') or '').split(':')[0]}")
             for r in records.read(ctx, "journal") if r.get("outcome") == "needs-judgement"]  # a park counts like a refusal
    for r in records.read(ctx, "refusal") + parks:
        n = counts.setdefault(r["code"], [0, 0])
        n[0] += 1
        if r.get("card") in landed and landed[r["card"]] >= r["at"]:
            n[1] += 1
            cards.setdefault(r["code"], set()).add(r["card"])
    lines = [f"{code:<24} {n[0]:>5} fired {n[1]:>5} on cards that landed after" for code, n in sorted(counts.items())]
    lines += [f"rule 7: {c} fired on {len(cards[c])} cards that landed anyway; make it a setting, sharpen it or retire it"
              for c in sorted(cards) if len(cards[c]) >= 2]
    return "\n".join(lines + [f"cards landed: {len(landed)}"])


def run(args, cwd):
    ctx = config.load(cwd)
    if args.tests:
        print(untouched(ctx))
        return 0
    if args.refusals:
        print(fired(ctx))
        return 0
    if budget.off(ctx):
        print("the line budget is off; list globs under core in [budgets] in ade.toml to turn it on")
        return 0
    rows = budget.measure(ctx)
    print(budget.render(rows))
    hard, _ = budget.over(rows)
    if hard:
        refuse("budget.over", f"over budget: {', '.join(r[0] for r in hard)}", "remove before adding, or move the budget in ade.toml")
