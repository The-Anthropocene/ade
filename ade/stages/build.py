# id: ade.stages.build
# what: build stage: the builder role makes the change in the lane (again, with the person's note, when its land question was answered 'rework: TEXT'); the driver commits it
# cmd: -
# reads: lane worktree, roles/builder.voice.md (or the card's profile voice), .ade/records.jsonl
# writes: lane commit (the change, with the lane's graph and generated skill regenerated), .ade/records.jsonl (a land question marked reworked)
import os
import subprocess
import sys
from datetime import date

from ade import budget, graph, host, install, providers, records
from ade.config import matches
from ade.gitx import git
from ade.stages import Result, land, lane_path, main, premise
from ade.verbs import decide


def _feed(ctx):
    need = ctx.cfg["graph"]["require"]
    parts = (["a test that imports it"] if "tests" in need else []) + (["the five-line header"] if "header" in need else [])
    return " and ".join(parts) or "nothing extra"


# The testing rule (CHARTER rule 10): tests are few, robust and documented, and assert what stays true.
TESTS = ("Tests: add one only where it guards a lasting invariant (a format, a contract, a property), never a "
         "literal example that is true only today; say in a comment what it guards. Fewer is better.")


def cap_line(ctx):
    """One line when the kit's test cap is full (the row exists only in the kit), so a full cap never means untested code."""
    for name, n, cap in budget.measure(ctx):
        if name == "test functions" and n >= cap:
            return (f"\n{name.capitalize()} {n} / {cap}: to add a test, replace or delete one the card's change makes "
                    f"redundant.")
    return ""


DONE = "DONE"


def said_done(answer):
    lines = [x.strip() for x in answer.splitlines() if x.strip()]
    return bool(lines) and lines[-1].strip("*`. ") == DONE


def met_evidence(answer):
    """The evidence after a last line `MET: ...`, or None when the builder did not end that way."""
    lines = [x.strip() for x in answer.splitlines() if x.strip()]
    last = lines[-1].strip("*`") if lines else ""
    return last[4:].strip() if last.startswith("MET:") else None


_told = []  # the stale-trunk-skill line prints once per run


def regenerate(ctx, lp):
    """Rewrite the lane's graph, and each generated skill copy init rendered there (a verb's help change would
    otherwise leave it stale). A lane of the kit renders both with its own ade, so a card that changes the graph code
    or a verb's help ships files that code agrees with; any other lane uses the running one, and leaves a skill copy
    alone when trunk's differs from it (trunk's is from another kit: only a person's `ade init` fixes that)."""
    skills = [lp / x for x in (install.SKILL, install.SKILL_CODEX) if (lp / x).is_file()]
    own = (lp / "ade" / "cli.py").is_file()
    if own and (lp / graph.ROUTE).is_file():
        subprocess.run([sys.executable, str(lp / "bin" / "ade"), "graph"], cwd=lp, check=True, capture_output=True)
    else:
        host.write(lp / ctx.cfg["graph"]["out"], graph.render(graph.build(lp, ctx.cfg)))
    if not skills:
        return
    if own:
        code = "import sys; from ade import install; sys.stdout.write(install.skill())"
        text = subprocess.run([sys.executable, "-c", code], cwd=lp, check=True, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(lp)}).stdout
    else:
        text = install.skill()
    for skill in skills:
        trunk = ctx.root / skill.relative_to(lp)
        if not own and trunk.is_file() and host.read(trunk) != text:
            if not _told:
                print("the generated skill on trunk is from another kit: a person runs `ade init`")
                _told.append(1)
            continue
        host.write(skill, text)


def project_rules(ctx, role, files, card=None):
    """The project's <role> voice (the card's profile voice in its place, if it names one), then one "dN: text" line per
    live standing ruling and per ruling whose paths match one of files, standing first, then newest first, up to
    [budgets] rulings of them; past that one line says how many more govern: a role that never runs the handover still
    works under the project's contract."""
    voice = ctx.root / providers.role_cfg(ctx, role, card).get("voice", f"roles/{role}.voice.md")
    live = [d for d in records.merged(ctx, "decision").values() if decide.state(d) == "ruling"]
    rules = [d for d in live if d.get("standing")][::-1] + [
        d for d in reversed(live) if not d.get("standing") and any(matches(f, d["paths"]) for f in files)]
    cap = ctx.cfg["budgets"]["rulings"]
    more = [f"{len(rules) - cap} more ruling{'s' * (len(rules) - cap > 1)} govern{'s' * (len(rules) - cap == 1)}: `ade decide --rulings`"] * (len(rules) > cap)
    return "".join(f"\n\n{x}" for x in ([host.read(voice).strip()] if voice.is_file() else [])
                   + [f"{d['id']}: {d['text']} {decide.by(d)}" for d in rules[:cap]] + more)


def rework(ctx, c):
    """The card's newest land question when a person answered it 'rework: TEXT' and the lane is not yet rebuilt."""
    qs = [d for d in records.merged(ctx, "decision").values() if d.get("card") == c["id"] and d.get("rec") == "land"]
    return qs[-1] if qs and land.rework_note(qs[-1]) is not None and not qs[-1].get("reworked") else None


def run(ctx, c):
    lp = lane_path(ctx, c["id"])
    rw = rework(ctx, c)
    # A landing cut off after its merge leaves main holding the lane, so main..HEAD is empty but the work is built.
    if (rw is None and int(git(lp, "rev-list", "--count", f"{main(ctx)}..HEAD"))) or land.merge_of(ctx, c):
        return Result("advance", "already built")
    prompt = (f"You are the ADE builder in {lp}. Card {c['id']}: {c['title']}{chr(10) + 'Why: ' + c['why'] if c.get('why') else ''}\nPaths: {', '.join(c['paths'])}\n"
              f"Done when: {c['done']}\nMake the smallest change that meets it. Do not commit; the driver commits.\n"
              f"Keep working until the done line is met; do not stop to ask or to check in unless you are blocked, "
              f"and then say what blocks you.\n"
              f"If the done line is already met at base with no change, make none and end with the last line "
              f"\"MET: <one line of evidence>\"; "
              f"use MET only when you changed no file, and if you edited anything, end with {DONE}.\n"
              f"Land checks that every code file you add or change has {_feed(ctx)}; add it in this change, "
              f"then run `ade graph`.\n{TESTS}\nRun tests, builds and anything long as `ade q run -- <command>`.\n"
              f"When the card's done line is met, end your answer with a last line: {DONE}\n"
              f"DONE means the card's edits are made: the driver regenerates the graph and the test stage runs the "
              f"tests, so a command you may not run is no reason to withhold {DONE} (say which in your answer).")
    blessed = ctx.cfg["graph"].get("blessed")
    if blessed:
        globs = ", ".join([blessed] if isinstance(blessed, str) else blessed)
        prompt += (f"\nBlessed helpers live in {globs}: search them and reuse a helper before writing a new one, and say "
                   f"in your answer when new code repeats a pattern that should become a blessed helper.")
    prompt += cap_line(ctx)
    if c.get("proofs"):
        prompt += f"\nland will run these on the merged main and they must exit 0: {'; '.join(c['proofs'])}"
    missing = premise(ctx, c, lp)
    if missing:
        prompt += (f"\nCheck the card first: its files do not yet contain {', '.join(missing)}. Fine if the card adds "
                   f"them; if it means existing code, its premise is wrong, so make the nearest change that meets its "
                   f"intent and say plainly what differed.")
    tracked = git(lp, "ls-files").splitlines()
    prompt += project_rules(ctx, "builder", c["paths"] + [f for f in tracked if matches(f, c["paths"])], c)
    answer = providers.call(ctx, "builder", prompt, lp, c)
    evidence = None if rw else met_evidence(answer)  # a built lane is never closed as met at base
    if evidence is not None and not git(lp, "status", "--porcelain"):
        records.append(ctx, "card", {"id": c["id"], "status": "closed", "closed_why": f"met at base: {evidence}",
                                     "closed_by": decide.who(), "closed_at": date.today().isoformat()})
        return Result("done", "closed: done line already met")
    # A builder cut off part-way (turn limit, lost session) leaves a half-built tree; it must not reach review.
    met = met_evidence(answer)  # MET after editing is a DONE claim: the lane has changes, so it advances
    if not said_done(answer) and met is None:
        return Result("needs-judgement", "the builder did not say DONE",
                      f"finish the card in {lp}, then `ade loop {c['id']}`")
    if not git(lp, "status", "--porcelain"):
        return Result("needs-judgement", "the builder changed nothing",
                      f"edit in {lp} (or reword the card), then `ade loop {c['id']}`")
    # The graph is derived, so the driver regenerates it rather than stopping when a builder forgot `ade graph`.
    if (lp / ctx.cfg["graph"]["out"]).is_file():
        regenerate(ctx, lp)
    git(lp, "add", "-A")
    git(lp, "commit", "-q", "-m", f"{c['id']}: {c['title']}")
    if rw:  # providers.call marked it delivered: the next loop reads it built, and land asks again
        return Result("advance", f"rework: {land.rework_note(rw)}")
    note = f" (builder ended MET: {met})" if met is not None and not said_done(answer) else ""
    return Result("advance", f"built with {providers.engine(ctx, 'builder', card=c)}{note}{providers.unhooked(c['id'])}")
