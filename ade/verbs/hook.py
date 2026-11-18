# id: ade.verbs.hook
# what: Claude Code hook entry: brief (first-edit briefing, protected paths), bash (the guard), read (secret Read/Grep under tracked: secret_ok), grep (graph notes on search output), turn-end (holds), start (handover)
# cmd: ade hook brief|bash|read|grep|turn-end|start  (stdin: the hook event's JSON)
# reads: stdin, graph.json, .ade/records.jsonl, .ade/seen/, .ade/kit.json
# writes: .ade/seen/<session>.txt and <session>.grep.txt, .ade/grep-notes.jsonl, .ade/records.jsonl (hold)
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ade import config, graph, guard, host, install, records
from ade.gitx import git
from ade.refuse import Refusal, refuse
from ade.verbs import decide, handover


def add(p):
    p.add_argument("name", choices=["brief", "bash", "read", "grep", "turn-end", "start"])


_NOTE = []  # one line when ade.toml would not load and the template's defaults stand in


def _say(event, **fields):
    if _NOTE and "additionalContext" in fields:
        fields["additionalContext"] = (_NOTE.pop() + "\n\n" + fields["additionalContext"]).strip()
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))


def _title(ctx, rel):
    # A prose file has no header; its first '# ' heading says what it is.
    if rel.endswith(".md"):
        try:
            for line in (ctx.root / rel).read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("# "):
                    return line[2:].strip() or "no header"
        except OSError:
            pass
    return "no header"


def briefing(ctx, rel, g=None):
    g = g or graph.load(ctx.root, ctx.cfg) or {"files": {}, "callers": {}, "tests": {}}
    f = g["files"].get(rel) or {}
    what = (f.get("header") or {}).get("what") or _title(ctx, rel)
    callers, tests = g["callers"].get(rel, []), g["tests"].get(rel, [])
    # Briefing order only: headed product code, then headerless scratch, then tests (each by path).
    shown = sorted(callers, key=lambda c: ((g["files"].get(c) or {}).get("test", False),
                                           not (g["files"].get(c) or {}).get("header"), c))
    decisions = [d for d in decide.unsettled(ctx)
                 if config.matches(rel, d["paths"]) and decide.state(d) != "retired"]
    rulings = [d for d in decisions if d.get("kind", "ruling") == "ruling"]
    decisions = [d for d in decisions if d not in rulings]
    from ade.stages import test as test_stage  # the stage owns the map
    units, mapnote = test_stage.unit_map(ctx, ctx.root, [rel], brief=True)
    tests = sorted(set(tests) | set(units)) if units else tests
    fallback = "the critical set runs" if ctx.cfg["tests"]["critical"] else "the whole suite runs"
    lines = [f"ade brief {rel} ({config.rigour([rel], ctx.cfg)}-grade): {what}"]
    lines += [mapnote] if mapnote else []
    by_dir = Counter(c.rsplit("/", 1)[0] + "/" if "/" in c else "./" for c in callers).most_common()
    rest = len(callers) - sum(n for _, n in by_dir[:3])
    left = len(by_dir) - 3
    tail = f", +{rest} in {left} folder{'s' * (left != 1)}" if rest else ""
    busy = f"{len(callers)} ({', '.join(f'{d} {n}' for d, n in by_dir[:3])}{tail})"
    lines += [f"callers: {busy if len(callers) > 8 else ', '.join(shown) or 'none'}",
             f"tests: {', '.join(tests[:6]) or f'none; {fallback}'}"]
    # Feeding the graph is told here and checked at land (ARCHITECTURE.md 4.3); editing is never blocked for it.
    need, noted = ctx.cfg["graph"]["require"], ctx.cfg["graph"].get("note", [])
    if f and not f.get("test") and not config.matches(rel, ctx.cfg["graph"]["untested_ok"]):
        if not tests and "tests" in need:
            lines.append("no test reaches this file; land will ask for one that imports it")
        elif not tests and "tests" in noted:
            lines.append("no test reaches this file; add one only for a lasting invariant (land notes it)")
        if "header" in need and not f.get("header"):
            lines.append("no five-line header (id, what, cmd, reads, writes); land will ask for one")
    # Scripts and route rows that run this verb or name this stage (ARCHITECTURE.md 12.6): told here, checked at land.
    stem = rel.rsplit("/", 1)[-1].removesuffix(".py").replace("_", "-")
    want = {"ade/verbs": f"ade {stem}", "ade/stages": f"stage:{stem}"}.get(rel.rsplit("/", 1)[0])
    bound = [s for s, b in g.get("binds", {}).items() if want in b] if want else []
    if bound:
        lines.append(f"run by {', '.join(bound[:4])}; if what it does at a stop changes, change them in this card")
    docs = g.get("docs", {}).get(rel, [])
    if docs:  # living docs first: shallow, undated paths before dated records
        docs = sorted(docs, key=lambda d: (bool(re.search(r"(^|/)\d{4}-\d\d", d)), d.count("/"), d))
        lines.append(f"docs that describe it (update them in the same change): {', '.join(docs[:4])}")
    made = graph.writers(g, rel)
    if made:
        lines.append(f"written by: {', '.join(made)}")
    again = graph.rewriters(g, rel)
    if again:
        lines.append(f"rewritten in place by {', '.join(again)}")
    gen = {}
    for y, n in graph.comoves(g, rel):
        gen.setdefault(n, []).append(y)
    lines += [f"generated from this by {n}: {', '.join(ys)}; regenerate them in the same change" for n, ys in gen.items()]
    lines += _reuse(ctx, g, rel, callers)
    lines += [f"decision {decide.line(d)}" for d in decisions[-4:]]
    # About 4 characters per token keeps the briefing inside its budget without a tokenizer; the ruling lines sit outside it.
    return "\n".join(_rulings(rel, rulings) + ["\n".join(lines)[: ctx.cfg["budgets"]["brief_tokens"] * 4]])


def _specificity(glob):
    # Fewest wildcards first, then the longest literal prefix.
    return (sum(glob.count(c) for c in "*?["), -len(re.split(r"[*?\[]", glob)[0]))


def _rulings(rel, rulings):
    """Governing rulings, most specific matching glob first, newest first within a tie: 'dN: text' under a RULES heading (whole to 400 characters, else the first sentence)."""
    if not rulings:
        return []
    ranked = sorted(enumerate(rulings), key=lambda t: (
        min(_specificity(g) for g in t[1]["paths"] if config.matches(rel, [g])), -t[0]))
    out = []
    for _, d in ranked[:6]:
        text = " ".join(d["text"].split())
        out.append(f"{d['id']}: {text if len(text) <= 400 else decide.first_sentence(text, 400)} {decide.by(d)}")
    if len(ranked) > 6:
        out.append(f"{len(ranked) - 6} more ruling{'s' * (len(ranked) > 7)} govern{'s' * (len(ranked) == 7)} {rel}")
    return [f"RULES for {rel} (follow these; ade decide --show dN for the full text):"] + out


# The first function a helper file names, by the graph's lang for it (ts covers JavaScript).
_FIRST_FN = {"py": r"^def ([a-z]\w*)", "ts": r"^export (?:async )?(?:function\*? |const )([A-Za-z_$][\w$]*)"}


def _reuse(ctx, g, rel, callers):
    """Blessed helpers ([graph] blessed plus the lib component's globs) that this file's neighbours, its callers and
    the files in its folder already import: at most three, most used first."""
    blessed = ctx.cfg.get("graph", {}).get("blessed") or []
    globs = [blessed] if isinstance(blessed, str) else list(blessed)
    globs += ctx.cfg["budgets"].get("components", {}).get("lib", {}).get("globs", [])
    here = rel.rsplit("/", 1)[0] if "/" in rel else ""
    near = [n for n, f in g["files"].items() if n != rel and (n in callers or n.rsplit("/", 1)[0] == here)]
    used = Counter(h for n in near for h in g["files"][n].get("imports", []) if h != rel and config.matches(h, globs))
    out = []
    for h, n in sorted(used.items(), key=lambda kv: (-kv[1], kv[0]))[:3]:
        pat = _FIRST_FN["ts" if g["files"].get(h, {}).get("lang") == "ts" else "py"]
        fn = re.search(pat, host.read(Path(ctx.root, h), errors="replace"), re.M)
        out.append(f"reuse: {h}{':' + fn.group(1) if fn else ''} (used by {n} files)")
    return out


# The files a Codex apply_patch envelope names; same section lines as ade/stages/review.py reads.
_PATCHED = re.compile(r"^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+?)\s*$", re.M)


def _command(data):
    # The command as one string: an argv list is joined, anything else that is not text is "".
    ti = data.get("tool_input")
    command = ti.get("command") if isinstance(ti, dict) else None
    if isinstance(command, list):
        command = " ".join(str(a) for a in command)
    return command if isinstance(command, str) else ""


def _patched(data):
    """Paths in an apply_patch call: tool_name apply_patch, or a Bash command that starts `apply_patch <<`."""
    command = _command(data)
    if not command:
        return []
    if data.get("tool_name") != "apply_patch" and not re.match(r"\s*apply_patch\s*<<", command):
        return []
    return _PATCHED.findall(command)


def _edit(ctx, data, cwd, top):
    ti = data.get("tool_input") or {}
    targets = _patched(data) or [ti.get("file_path") or ti.get("notebook_path")]
    texts = []
    for target in filter(None, targets):
        # An absolute path (the real model sends one) is made repo-relative here.
        rel = Path(os.path.relpath(Path(cwd, target).resolve(), top)).as_posix()
        if rel.startswith(".."):
            continue
        if config.matches(rel, ctx.cfg["protected"]["paths"]):
            refuse("edit.protected", f"{rel} is protected config",
                   guard.cure(ctx.cfg, rel))
        texts.append(_first(ctx, data, rel))
    texts = [t for t in texts if t]
    if texts:
        _say("PreToolUse", additionalContext="\n\n".join(texts))


def _first(ctx, data, rel):
    # The briefing for rel the first time this session touches it (Edit or Bash alike), else "".
    seen = ctx.state / "seen" / f"{data.get('session_id', 'none')}.txt"
    seen.parent.mkdir(parents=True, exist_ok=True)
    if rel in (host.read(seen).splitlines() if seen.is_file() else []):
        return ""
    with host.opentext(seen, "a") as fh:
        fh.write(rel + "\n")
    return briefing(ctx, rel)


def _bash(ctx, data, cwd, top):
    branch = git(top, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    command, here = _command(data), str(Path(cwd).resolve())
    guard.check(ctx.cfg, command, str(top), here, branch)  # a refusal comes before any briefing
    if _patched(data):  # a Bash `apply_patch <<` envelope: the Edit check (protected paths, briefing)
        return _edit(ctx, data, cwd, top)
    texts = [t for t in (_first(ctx, data, rel) for rel in guard.writes(ctx.cfg, command, str(top), here)) if t]
    if texts:
        _say("PreToolUse", additionalContext="\n\n".join(texts))


def _read(ctx, data, cwd, top):
    # Read and Grep of a secret file: the static deny cannot carry a tracked: secret_ok exception, so judge it here.
    ti = data.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("path")
    here = str(Path(cwd).resolve())
    if target and guard.secret(ctx.cfg, target, str(top), here):
        refuse("guard.secret", f"{target} is a secret file", "pass the file by path to the tool that needs it; never print it")
    # A Grep over a directory (or no path) reads every file under it: refuse when its glob or type names a secret.
    if not (target and Path(here, target).is_file()):
        names = [Path(ti["glob"]).name] if ti.get("glob") else []
        names += ["x." + ti["type"]] if ti.get("type") else []
        if any(guard.secret(ctx.cfg, n, str(top), here) for n in names):
            refuse("guard.secret", "this Grep would search secret files", "name the file you need by path; never print secrets")


# Programs whose output is a search result; only the program name is read, never its pattern, flags or chain.
SEARCHERS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr", "Select-String", "sls"}
# Where a path ends in a result line: path:12:text, path-12-text (context), path:3 (-c). A bare path is the whole line.
_CUT = re.compile(r"[:-]\d+(?:[:-]|$)")


def searched(command):
    return any(guard._name(seg[0]) in SEARCHERS or [guard._name(t) for t in seg[:2]] == ["git", "grep"]
               for seg in guard.segments(command) if seg)


def paths_in(text, files, cwd, top):
    """Graph files named at the start of result lines, with how many lines each had. Only the output is read: a
    candidate counts when it is a file the graph knows, so a cut line, a binary notice or a count is silent."""
    hits, base = Counter(), Path(cwd).resolve()
    for line in text.splitlines():
        line = line[:1000].strip()
        for cand in [line] + [line[:m.start()] for m in _CUT.finditer(line)]:
            if not cand:
                continue
            try:
                rel = Path(os.path.relpath(os.path.normpath(os.path.join(base, cand)), top)).as_posix()
            except ValueError:
                continue  # Windows: a candidate on another drive (or `C` of `C:\`) is not in this repo
            if rel in files:
                hits[rel] += 1
                break
    return hits


def _reach(g, rel):
    # Blast radius: every file that imports this one, directly or through another.
    seen, todo = set(), [rel]
    while todo:
        for c in g["callers"].get(todo.pop(), []):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return len(seen)


def note_line(ctx, g, rel, deadline):
    f, callers, tests = g["files"][rel], g["callers"].get(rel, []), g["tests"].get(rel, [])
    parts = [f"{rel}: {((f.get('header') or {}).get('what') or 'no header')[:100]}",
             f"{len(callers)} callers, reach {_reach(g, rel)}", f"tests: {', '.join(tests[:2]) or 'none'}"]
    # Last commit is the slow, load-sensitive part; it is skipped once the budget is spent.
    left = deadline - time.monotonic()
    if left > 0:
        try:
            r = git(ctx.root, "log", "-1", "--format=%cs %s", "--", rel, check=False, timeout=left)
            if r.returncode == 0 and r.stdout.strip():
                parts.append(f"last: {r.stdout.strip()[:60]}")
        except subprocess.TimeoutExpired:
            pass
    return " · ".join(parts)


def _text(resp):
    # Bash gives stdout; the native Grep tool gives content or a filenames list.
    if isinstance(resp, str):
        return resp
    resp = resp if isinstance(resp, dict) else {}
    return "\n".join([str(resp.get(k) or "") for k in ("stdout", "content")] + list(resp.get("filenames") or []))


def _rows(log):
    out = []
    try:
        with open(log, "rb") as fh:  # only the last 256 KB, so the cost is bounded whatever the log's size
            size = fh.seek(0, 2)
            fh.seek(max(0, size - 262144))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()[1 if size > 262144 else 0:]
    except OSError:
        lines = []  # the first partial line is dropped above
    for raw in lines:
        try:
            out.append(json.loads(raw))
        except ValueError:
            pass
    return [r for r in out if isinstance(r, dict)]


def _grep(ctx, data, cwd, top):
    # Context only: the search ran as typed and its output is untouched; this adds a note after it or says nothing.
    tool, ti, session = data.get("tool_name"), data.get("tool_input") or {}, data.get("session_id", "none")
    log, seen = ctx.state / "grep-notes.jsonl", ctx.state / "seen" / f"{session}.grep.txt"
    shown = host.read(seen).splitlines() if seen.is_file() else []
    command = ti.get("command")
    if isinstance(command, list):
        command = " ".join(str(a) for a in command)  # Codex may give argv
    # What the session opened: the Read tool's file, or the file words of a Bash reader (never a pattern or flag).
    opened = [ti.get("file_path") or ""] if tool == "Read" else []
    for seg in guard.segments(command) if command is not None else []:
        if seg and guard._name(seg[0]) in set(ctx.cfg["guards"]["readers"]) - SEARCHERS:
            opened += [a for a in seg[1:] if not a.startswith("-")]
    if opened:
        # Counts against a note shown in the same lane (worktree top), by this session or another (the reviewer).
        lane = set()
        for r in _rows(log):
            if (r.get("top") or r.get("cwd")) == str(top) and isinstance(r.get("shown"), list):
                t = r.get("tests") or []
                lane |= set(r["shown"]) | set(sum(t.values(), []) if isinstance(t, dict) else t)
        for target in opened:
            rel = Path(os.path.relpath(Path(cwd, target).resolve(), top)).as_posix()
            if rel in lane:
                with host.opentext(log, "a") as fh:
                    fh.write(json.dumps({"at": int(time.time()), "session": session, "top": str(top), "read": rel}) + "\n")
    if tool == "Read":
        return
    if tool != "Grep" and (tool == "Bash" or command is not None) and not searched(command or ""):
        return  # Bash, or an unknown tool that carries a command, is read like Bash
    g = graph.load(ctx.root, ctx.cfg)
    if not g:
        return
    hits = paths_in(_text(data.get("tool_response") or data.get("tool_output")), g["files"], cwd, top)
    fresh = [p for p, _ in hits.most_common() if p not in shown]
    if not fresh:
        return
    cap, deadline = ctx.cfg["budgets"]["grep_files"], time.monotonic() + 1.0
    lines = [note_line(ctx, g, p, deadline) for p in fresh[:cap]]
    if len(fresh) > cap:
        lines.append(f"+{len(fresh) - cap} more files matched; notes cover the top {cap}")
    seen.parent.mkdir(parents=True, exist_ok=True)
    with host.opentext(seen, "a") as fh:
        fh.write("".join(p + "\n" for p in fresh[:cap]))
    with host.opentext(log, "a") as fh:
        fh.write(json.dumps({"at": int(time.time()), "session": session, "top": str(top), "shown": fresh[:cap],
                             "tests": {p: g["tests"].get(p, [])[:2] for p in fresh[:cap]}}) + "\n")
    _say("PostToolUse", additionalContext="ade graph notes (context, not a ruling):\n"
         + "\n".join(lines)[: ctx.cfg["budgets"]["brief_tokens"] * 4])


def _turn_end(ctx, data, cwd, top):
    # What the turn ends waiting on, from the journal: a judgement stop waits on a person, any other stop on a machine.
    last = (records.read(ctx, "journal") or [None])[-1]
    if not last or last["outcome"] not in ("wait", "needs-judgement"):
        return
    kind = "person" if last["outcome"] == "needs-judgement" else "machine"
    hold = {"session": data.get("session_id", "none"), "card": last["card"], "waits_on": last["next"], "kind": kind}
    held = [h for h in records.read(ctx, "hold") if h["session"] == hold["session"]]
    if not held or {k: held[-1].get(k) for k in hold} != hold:
        records.append(ctx, "hold", hold)


HOOK_CAP, HOOK_TAIL = 10000, 1800  # the cap, and room kept for the lines the hook adds after the handover


def _start(ctx, data, cwd, top):
    # A cold start and a post-compaction re-entry get the same briefing; lanes set ADE_ROLE for their builder.
    role = os.environ.get("ADE_ROLE") or "pilot"
    if os.environ.get("ADE_MARK"):  # a lane's call asks for proof that ADE's hooks ran in it
        host.write(Path(os.environ["ADE_MARK"]), "")
    if role != "pilot" and data.get("source") == "resume":
        return  # a lane only resumes a session (a forked review, a retry) that already holds its handover
    body = handover.text(ctx, role if role in ctx.cfg["roles"] else "pilot")
    if len(body) > HOOK_CAP:  # Claude Code shows only a 2 KB preview of hook context over 10,000 characters
        head, *secs = body.split("\n\n## ")
        note = f"\n\n(more sections: `ade handover --role {role}` prints the rest)"
        body = head
        for i, sec in enumerate(secs):
            if len(body) + len(sec) + 5 + len(note) + HOOK_TAIL > HOOK_CAP:
                body += note
                break
            body += "\n\n## " + sec
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    recent = sorted((d for d in records.merged(ctx, "decision").values() if d.get("kind") == "question"
                     and d.get("answerer") != "agent" and d.get("by") == "agent" and d.get("at", "") >= week),
                    key=lambda d: d["at"], reverse=True)
    body += "".join(f"\n{d['id']}: answered by an agent, a question meant for a person" for d in recent[:3])
    kit = ctx.state / "kit.json"
    try:
        made = json.loads(host.read(kit))["kit"] if kit.is_file() else None
    except (ValueError, KeyError, TypeError):
        made = None
    if made and made != install.kit():
        body += (f"\nhooks run ade {install.kit()}; this repo was set up with {made}: "
                 "rerun ade init from the kit you mean")
    news = ctx.state / "news.md"
    text = host.read(news) if role == "pilot" and news.is_file() else ""  # news is the pilot's, not a lane's
    if text and time.time() - news.stat().st_mtime < 7 * 86400:  # shown for a week after the init that wrote it
        old, _, rest = text.partition("\n")
        cap = 1200  # a few hundred tokens
        more = "\n(cut; .ade/news.md has the rest)" if len(rest) > cap else ""
        body += f"\n\n## What changed in ADE since {old[11:-4]}\n{rest[:cap]}{more}\n"
    _say("SessionStart", additionalContext=body)


HANDLERS = {"brief": _edit, "bash": _bash, "read": _read, "grep": _grep, "turn-end": _turn_end, "start": _start}


def run(args, cwd):
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        data = json.loads(raw or "{}")  # bytes as UTF-8, not the locale's; a text-only stdin is already decoded
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0
    cwd = data.get("cwd") or cwd
    try:
        top = config.toplevel(cwd)
    except Refusal:
        return 0  # no git on PATH: a hook has nothing to judge and must never block
    if top is None:
        return 0
    try:
        ctx = config.load(cwd)
    except (Refusal, tomllib.TOMLDecodeError) as e:
        root = config.main_worktree(cwd)
        if root is None or not (root / "ade.toml").is_file():
            return 0  # not an ADE repo: stay silent
        if args.name not in ("brief", "bash", "read"):
            return 0
        ctx = config.load_at(root, own=False)  # ade.toml is broken: keep the default guard fence up and say so
        why = e.cause if isinstance(e, Refusal) else f"parse error: {e}"
        _NOTE.append(f"ade.toml did not load ({why}); guarding with the bundled defaults. Fix ade.toml.".replace("\n", " "))
    if args.name == "grep":
        try:
            _grep(ctx, data, cwd, top)
        except Exception:
            pass  # the grep note is context only: any failure is silent and the search result stands
        return 0
    try:
        HANDLERS[args.name](ctx, data, cwd, top)
    except Refusal as r:
        if args.name in ("brief", "bash", "read"):
            _say("PreToolUse", permissionDecision="deny",
                 permissionDecisionReason=(_NOTE.pop() + "\n" if _NOTE else "") + r.render())
        records.fired(ctx, r)  # after the deny, so the guard holds whatever the row does
    if _NOTE:
        _say("PreToolUse", additionalContext="")
    return 0
