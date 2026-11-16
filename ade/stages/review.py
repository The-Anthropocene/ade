# id: ade.stages.review
# what: review stage: none for prose, else one light review; the prompt names files outside the card's paths for the reviewer to judge; the reviewer approves, edits the lane (committed), or returns a patch the driver applies once
# cmd: -
# reads: .ade/records.jsonl, roles/reviewer.voice.md (or the card's profile voice), lane diff, .ade/review-parent.json, CHARTER.md, ARCHITECTURE.md
# writes: .ade/records.jsonl (review, review_verdict), .ade/reviews/<card>-<tree12>.patch (a stopped patch, whole), lane commit (review patch), .ade/review-parent.json
import hashlib
import json
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import NamedTuple, Optional

from ade import host, parts, providers, records
from ade.config import matches
from ade.gitx import git
from ade.refuse import Refusal
from ade.stages import Result, build, cached, lane_path, main, touched, tree


def shell_quote(arg):
    """Quote one argument for the shell that will run it: cmd.exe on Windows, else a POSIX shell."""
    if os.name == "nt":
        return '"' + arg.replace('"', '""').replace("%", "%%") + '"'
    return shlex.quote(arg)


def policy(ctx, paths):
    # One light round everywhere; proof-grade paths get their extra care from tests.
    return "none" if all(matches(p, ctx.cfg["rigour"]["no_review"]) for p in paths) else "light"


def approves(text):
    """True only when the first line is APPROVE itself, alone or followed by punctuation: "APPROVE is not warranted"
    starts with the word and is a refusal (the first two-lane run, 28 Oct, landed one as an approval)."""
    lines = text.strip().splitlines() or [""]
    if re.match(r"approved?(\W*$|\s*[.!:,;\u2014-])", lines[0].strip(" *_`#"), re.I):
        return True
    # A preamble (e.g. "I cannot run commands") may come first: with no diff, a later line of APPROVE alone counts.
    return patch_of(text) is None and any(re.fullmatch(r"approved?\W*", x.strip(" *_`#"), re.I) for x in lines)


def patch_of(text):
    """The unified diff in a reviewer's answer, from its first `diff --git` or `--- ` line to a closing ``` fence; None if there is none."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, x in enumerate(lines) if x.startswith(("diff --git ", "--- ")) or x.strip() == "*** Begin Patch"), None)
    if start is None:
        return None
    # rstrip only: a diff line always has a leading ' ', '+' or '-', so a bare fence is never a hunk line.
    # A Codex apply_patch envelope ends at its "*** End Patch" line, which that line closes too.
    stop = next((i for i in range(start, len(lines)) if lines[i].rstrip() == "```" or lines[i].strip() == "*** End Patch"), None)
    end = len(lines) if stop is None else stop + (lines[stop].strip() == "*** End Patch" and lines[start].strip() == "*** Begin Patch")
    body = "".join(lines[start:end])
    return body if body.endswith("\n") else body + "\n"


def apply_envelope(lp, patch):
    """Apply a Codex apply_patch envelope in the lane: each Update hunk's context and removed lines are found in the
    file, in order, and replaced by its context and added lines; Add creates a file, Delete removes one. Nothing is
    written unless every section resolves; ValueError names what was not found."""
    sections, cur = [], None
    for x in patch.splitlines():
        m = re.match(r"\*\*\* (Update|Add|Delete) File: (.+?)\s*$", x)
        if m:
            cur = (m[1], m[2], [[]])
            sections.append(cur)
        elif cur and x.startswith("@@"):
            cur[2].append([])
        elif cur and x[:1] in (" ", "+", "-", ""):
            cur[2][-1].append(x)
    out = {}
    for kind, name, hunks in sections:
        p = (lp / name).resolve()
        if not p.is_relative_to(Path(lp).resolve()):
            raise ValueError(f"{name} is outside the lane")
        if kind == "Delete":
            out[p] = None
        elif kind == "Add":
            out[p] = "".join(x[1:] + "\n" for h in hunks for x in h if x[:1] == "+")
        else:
            if not p.is_file():
                raise ValueError(f"{name} is not a file")
            lines, at = host.read(p).split("\n"), 0
            for h in (h for h in hunks if h):
                old = [x[1:] for x in h if x[:1] != "+"]
                new = [x[1:] for x in h if x[:1] != "-"]
                i = next((i for i in range(at, len(lines) - len(old) + 1)
                          if [y.rstrip() for y in lines[i:i + len(old)]] == [y.rstrip() for y in old]), None) if old else len(lines) - (lines[-1] == "")
                if i is None:
                    raise ValueError(f"the hunk's lines are not found in {name}: {old[0] if old else ''}")
                lines[i:i + len(old)] = new
                at = i + len(new)
            out[p] = "\n".join(lines)
    for p, text in out.items():
        if text is None:
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            host.write(p, text)


def without(patch, name):
    """The patch less every file section for `name` (a generated file is rendered in the lane, never patched in); a
    section opens at `diff --git` or at a `--- ` line followed by `+++ `."""
    out, keep = [], True
    lines = patch.splitlines(keepends=True)
    for i, x in enumerate(lines):
        if x.startswith("diff --git "):
            keep = x.split()[-1] != "b/" + name
        elif x.startswith("--- ") and lines[i + 1:i + 2] and lines[i + 1].startswith("+++ ") and (i == 0 or not lines[i - 1].startswith(("diff --git ", "index ", "new file", "deleted file", "similarity", "rename"))):
            keep = x[4:].split("\t")[0].strip() not in (f"a/{name}", name)
        if keep:
            out.append(x)
    return "".join(out)


def outside(ctx, c, pf, lp):
    """Paths the patch touches that a builder could not: protected config, or anything outside the card's paths
    and the graph file (the build stage's return contract). The driver applies patches past the edit hook."""
    r = git(lp, "apply", "--numstat", str(pf), check=False)
    return strays(ctx, c, [line.split("\t", 2)[2] for line in r.stdout.splitlines() if line.count("\t") >= 2])


def strays(ctx, c, names):
    from ade.stages.land import unrendered  # land imports review
    prot = [p for p in names if matches(p, ctx.cfg["protected"]["paths"])]
    bad = set(unrendered(ctx, lane_path(ctx, c["id"]), prot, c))
    return [p for p in names if p in bad or not (p in prot or matches(p, c["paths"]) or p == ctx.cfg["graph"]["out"])]


CONTRACT = ("Approve when the done line is met and the change is fit for its purpose. Refuse only for a real defect or "
            "oversight. A missing test, pin, extra check or polish is advice, never a reason to refuse; a needless new "
            "test, pin or check is itself a finding; a removal that keeps the card's outcome is a win, not a weakening.")
STALE = 55 * 60  # a prompt cache lapses an hour after its last hit, and each fork is a hit, so the clock restarts at the last use; stay clear of the edge


class Pick(NamedTuple):
    name: Optional[str] = None
    why: Optional[str] = None  # the fallback reason, if any
    no_opposite: bool = False  # the builder engine has no opposite, so the reason is printed


def pick(ctx, c):
    """Pick(provider name or None, fallback reason or None, no_opposite flag). With [review] engine = "opposite", the roster's other engine
    (claude <-> codex) than the card's latest build usage row; the same engine, with the reason, when that one is
    not on PATH or has no reviewer model. ADE_PROVIDER forces every role, so it ends the choice."""
    if ctx.cfg["review"].get("engine", "same") != "opposite" or os.environ.get("ADE_PROVIDER"):
        return Pick()
    built = [u["provider"] for u in records.read(ctx, "usage") if u.get("card") == c["id"] and u.get("stage") == "build"]
    other = {"claude": "codex", "codex": "claude"}.get(built[-1]) if built else None
    if built and not other:
        return Pick(None, f"no opposite for {built[-1]}, using {providers.engine(ctx, 'reviewer', card=c)}", True)
    if not other:
        return Pick(None, f"no claude or codex build row for {c['id']}")
    cmd = ctx.cfg["providers"].get(other, {}).get("cmd") or [""]
    if not providers.role_cfg(ctx, "reviewer", c)["models"].get(other):
        return Pick(None, f"{other} has no reviewer model")
    if not shutil.which(cmd[0]):
        return Pick(None, f"{other} is not on PATH")
    return Pick(other)


def _call(ctx, c, prompt, lp, name=None):
    """The reviewer call. With [review] fork on and fork arguments on the provider, each review forks one parent a
    day (it read CHARTER.md and ARCHITECTURE.md once); a new day, changed files or a parent last used over STALE
    seconds ago (its cache has lapsed) start a new parent, and a failed
    fork goes cold and forgets the parent, so the next review starts afresh rather than failing the same fork again."""
    args = providers.fork_args(ctx, "reviewer", name, c) if ctx.cfg["review"]["fork"] else None
    reads = [f for f in ctx.cfg["review"].get("fork_reads", ["CHARTER.md", "ARCHITECTURE.md"]) if (ctx.root / f).is_file()]
    if not args or not reads:
        return providers.call(ctx, "reviewer", prompt, lp, c, name=name)
    new, fork = args
    fill = lambda argv, s: [a.replace("{session}", s) for a in argv]
    read = b"".join(p.read_bytes() for p in (ctx.root / f for f in reads))
    state = ctx.state / "review-parent.json"
    mine = {"day": time.strftime("%Y-%m-%d"), "hash": hashlib.sha256(read).hexdigest(),
            "engine": providers.engine(ctx, "reviewer", name, c)}
    try:
        have = json.loads(host.read(state)) if state.is_file() else {}
        if {k: have.get(k) for k in mine} != mine or time.time() - have.get("used", 0) > STALE:
            sid = str(uuid.uuid4())
            providers.call(ctx, "reviewer", f"Read {' and '.join(reads)}, then answer READY and nothing else.",
                           lp, c, fill(new, sid), sid, name)
            have = {**mine, "session": sid, "used": time.time()}
            host.write(state, json.dumps(have) + "\n")
        text = providers.call(ctx, "reviewer", prompt, lp, c, fill(fork, have["session"]), have["session"], name)
        host.write(state, json.dumps({**have, "used": time.time()}) + "\n")
        return text
    except (Refusal, ValueError):
        state.unlink(missing_ok=True)
        return providers.call(ctx, "reviewer", prompt, lp, c, name=name)


BIG_DIFF = 200_000  # chars: over this the prompt carries a summary, not the diff (Codex takes at most 1,048,576)
FILE_CUT = 4_000    # chars of each changed file's own diff kept in a summary
SUMMARY_CAP = 400_000


def summarised(lp, base, diff):
    """`diff` itself when small; else git diff --stat plus each file's diff cut to its first FILE_CUT chars, under SUMMARY_CAP."""
    if len(diff) <= BIG_DIFF:
        return diff
    stat = git(lp, "diff", "--stat", f"{base}...HEAD")
    out = [f"NOTE: the diff was too big to embed, so it is summarised: the stat, then each file's diff cut. Read the rest in the lane "
           f"worktree ({lp}) with `git diff {base}...HEAD -- <path>`.\n", stat]
    names = [x for x in git(lp, "diff", "--name-only", f"{base}...HEAD").split("\n") if x]
    # Every file keeps an equal share of the cap (less a reserve for the "not shown" line); past the 200-char floor
    # the files that no longer fit are named in one line and their diffs are not run.
    stat = out[1] = stat[:SUMMARY_CAP // 4]
    room = SUMMARY_CAP - 2_000 - len(out[0]) - len(stat) - 2
    cut = max(200, min(FILE_CUT, room // max(1, len(names)) - 100))
    used = 0
    for i, n in enumerate(names):
        if used + cut + 110 + len(n) > room:
            out.append((f"not shown, {len(names) - i} files: " + ", ".join(names[i:]))[:1_900])
            break
        d = git(lp, "diff", f"{base}...HEAD", "--", n)
        if len(d) > cut:
            kept = d[:cut].rsplit("\n", 1)[0]
            d = f"{kept}\n… cut, {d.count(chr(10)) - kept.count(chr(10))} more lines ({n})"
        out.append(d)
        used += len(d) + 1
    return "\n".join(out)


def record(ctx, c, full):
    """The full review row stays local; the trimmed verdict row (no text, no patch) is the durable one."""
    records.append(ctx, "review", full)
    eng = full["engine"]
    records.append(ctx, "review_verdict", {
        "card": full["card"], "verdict": full["result"], "engine": eng,
        "model": providers.role_cfg(ctx, "reviewer", c)["models"].get(eng),
        "effort": providers.effort_level(ctx, "reviewer", eng, c), "edited": "yes" if full.get("edits") or (full["result"] == "approve" and "patch" in full) else "no",
        **({"fallback": full["fallback"]} if full.get("fallback") else {})})


def run(ctx, c):
    lp = lane_path(ctx, c["id"])
    t = tree(lp)
    if cached(ctx, "review", c, t, "approve"):
        return Result("advance", "review cached for this tree")
    pol = policy(ctx, touched(ctx, c))
    name, why, no_opp = pick(ctx, c)
    if no_opp:
        print(f"opposite review: {why}")
    row = {"card": c["id"], "policy": pol, "engine": providers.engine(ctx, "reviewer", name, c),
           **({"fallback": why} if why else {})}
    if "review" in parts.off(ctx.cfg):
        # The review part is off: a skipped review is recorded as one, so land accepts the tree without a reviewer.
        record(ctx, c, {**row, "policy": "off", "tree": t, "result": "approve", "text": "skipped: review is off"})
        return Result("advance", "review is off: skipped")
    if pol == "none":
        record(ctx, c, {**row, "tree": t, "result": "approve", "text": ""})
        return Result("advance", "prose only: no review")
    diff = git(lp, "diff", f"{main(ctx)}...HEAD")
    files = git(lp, "diff", "--name-only", f"{main(ctx)}...HEAD").split("\n")
    diff = summarised(lp, main(ctx), diff)
    odd = [] if ctx.cfg["land"]["strays"] == "off" else strays(ctx, c, [f for f in files if f])
    head = (f"Review card {c['id']} ({c['title']}{'; why: ' + c['why'] if c.get('why') else ''}; done when: {c['done']}). If something is wrong, fix it directly in the lane's "
              f"files (do not commit) and end with a line CHANGED; after editing, answer APPROVE on the first line if the card is now done, "
              f"or say what is still wrong. With no edits, answer APPROVE on the first line if it is fine. "
              f"Only if you cannot edit files, give a unified diff instead (paths relative to the repo root). "
              f"The card's paths are: {', '.join(c['paths'])}. Change only these; anything else that needs changing goes after APPROVE as a note."
              f"{' The lane also changes files outside them, which land will note (judge whether each belongs): ' + ', '.join(odd) + '.' if odd else ''}\n{CONTRACT}{build.project_rules(ctx, 'reviewer', files, c)}")
    prompt = f"{head}\n\n{diff}"
    text = _call(ctx, c, prompt, lp, name)
    # A review cut off after its verdict line (e.g. a bare "not ready for approval:") with no lane edits gets one more call.
    if not approves(text) and len(text.strip().splitlines()) <= 1 and not git(lp, "status", "--porcelain", check=False).stdout.strip():
        text = _call(ctx, c, prompt, lp, name)
    cure = f"read it with `ade card show {c['id']}`, apply in {lp}, commit, then `ade loop {c['id']}`"
    # A reviewer with tools may edit the lane directly: the same outside-the-card rule as its patch, then commit.
    for rnd in (1, 2):
        dirty = [x[3:].split(" -> ")[-1].strip('"') for x in
                 git(lp, "status", "--porcelain", "--untracked-files=all", check=False).stdout.splitlines()]
        if dirty:
            bad = strays(ctx, c, dirty)
            if bad:
                record(ctx, c, {**row, "tree": t, "result": "changes", "text": text[-4000:]})
                return Result("needs-judgement", f"the reviewer edited outside the card: {', '.join(bad)}", cure)
            build.regenerate(ctx, lp)
            git(lp, "add", "-A")
            git(lp, "commit", "-q", "-m", f"{c['id']}: review edits")
            # Edits stay committed, but they approve only when the text answers APPROVE; a patch in the text is ignored.
            if not approves(text) and rnd == 1:
                # Committed edits that did not approve get one re-review of the new diff with the same prompt.
                diff = summarised(lp, main(ctx), git(lp, "diff", f"{main(ctx)}...HEAD"))
                text = _call(ctx, c, f"{head}\n\n{diff}", lp, name)
                continue
            if not approves(text):
                record(ctx, c, {**row, "tree": tree(lp), "result": "changes", "edits": dirty, "text": text[-4000:]})
                return Result("needs-judgement", (text.strip().splitlines() or [""])[0] or "the review edited the lane and did not approve", cure)
            record(ctx, c, {**row, "tree": tree(lp), "result": "approve", "edits": dirty, "text": text[-4000:]})
            return Result("advance", "light review: edits applied")
        break
    if rnd == 2 and not approves(text) and not dirty:
        record(ctx, c, {**row, "tree": tree(lp), "result": "changes", "text": text[-4000:]})
        return Result("needs-judgement", (text.strip().splitlines() or [""])[0] or "the re-review did not approve", cure)
    if approves(text):
        record(ctx, c, {**row, "tree": tree(lp), "result": "approve", "text": text[-4000:]})
        return Result("advance", "light review approved")
    patch = patch_of(text)
    if patch is None:
        record(ctx, c, {**row, "tree": t, "result": "changes", "text": text[-4000:]})
        return Result("needs-judgement", "the review answered neither APPROVE nor a patch", cure)
    env = patch.startswith("*** Begin Patch")
    patch = patch if env else without(patch, ctx.cfg["graph"]["out"])
    if not patch.strip():
        record(ctx, c, {**row, "tree": tree(lp), "result": "approve", "text": text[-4000:]})
        return Result("advance", "light review approved (its patch was only the graph)")
    pf = Path(lp, git(lp, "rev-parse", "--git-path", "ade-review.patch"))
    host.write(pf, patch)
    # A stop on the patch keeps it whole under the main state (the lane's git dir goes with the lane).
    kept = ctx.state / "reviews" / f"{c['id']}-{t[:12]}.patch"
    kept.parent.mkdir(parents=True, exist_ok=True)
    whole = {"patch_file": str(kept)}
    cure_whole = f"{cure}; the whole patch: {kept}"
    try:
        stray = (strays(ctx, c, re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", patch, re.M)) if env
                 else outside(ctx, c, pf, lp))
        if stray:
            host.write(kept, patch)
            record(ctx, c, {**row, "tree": t, "result": "changes", "patch": patch[-4000:], **whole,
                                           "text": text[-4000:]})
            # The card's paths are fixed: the way on is to close it and add it again with the paths the change needs.
            flags = " ".join(f"--path {shell_quote(p)}" for p in [*c["paths"], *stray])
            readd = (f"close it and add it again with the paths the change needs: ade card close {c['id']} "
                     f"--why \"paths too narrow\" && ade card add {shell_quote(c['title'])} {flags} "
                     f"--done {shell_quote(c['done'])}")
            return Result("needs-judgement", f"the review patch reaches outside the card: {', '.join(stray)}",
                          f"{cure_whole}; {readd}")
        # --ignore-whitespace: a model writes LF diffs, and a Windows checkout may hold CRLF files.
        # --recount: a model often miscounts the lines in a hunk header; git recounts from the body.
        why = ""
        if env:
            try:
                apply_envelope(Path(lp), patch)
            except ValueError as e:
                why = str(e)
        else:
            chk = git(lp, "apply", "--check", "--ignore-whitespace", "--recount", str(pf), check=False)
            why = next((x.strip() for x in chk.stderr.splitlines() if x.strip()), "") if chk.returncode else ""
        if why:
            host.write(kept, patch)
            record(ctx, c, {**row, "tree": t, "result": "changes", "patch": patch[-4000:], **whole,
                                           "text": text[-4000:]})
            return Result("needs-judgement", f"the review patch does not apply: {why}", cure_whole)
        if not env:
            git(lp, "apply", "--ignore-whitespace", "--recount", str(pf))
    finally:
        pf.unlink(missing_ok=True)
    build.regenerate(ctx, lp)
    git(lp, "add", "-A")
    git(lp, "commit", "-q", "-m", f"{c['id']}: review patch")
    # Applied once and approved as applied; the test stage proves the patched tree, and there is no second round.
    record(ctx, c, {**row, "tree": tree(lp), "result": "approve", "patch": patch[-4000:],
                                   "text": text[-4000:]})
    return Result("advance", "light review: patch applied")
