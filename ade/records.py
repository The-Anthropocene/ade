# id: ade.records
# what: append-only typed JSONL records; only verbs call append, everything else reads
# cmd: -
# reads: .ade/records.jsonl, .ade/records-archive.jsonl
# writes: .ade/records.jsonl, .ade/records-archive.jsonl (old non-durable rows), durable rows also to records.jsonl on the ade-records branch (not from inside a lane: ADE_ROLE set)
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from ade import host
from ade.gitx import git

# The facts we would be sorry to lose (ARCHITECTURE.md 3.2); the rest is a cache a rerun rebuilds.
# Usage rows are not in it: no usage numbers leave the machine, only the share flag may.
DURABLE = {"card", "decision", "observation", "outcome", "refusal", "residual", "share", "test_red", "review_verdict"}
BRANCH, FILE = "ade-records", "records.jsonl"
# Record commits carry the kit's own identity, so a machine without git user config still keeps its records.
WHO = {"GIT_AUTHOR_NAME": "ade", "GIT_AUTHOR_EMAIL": "ade@localhost",
       "GIT_COMMITTER_NAME": "ade", "GIT_COMMITTER_EMAIL": "ade@localhost"}


def _path(ctx):
    return ctx.state / "records.jsonl"


def _adopt(ctx):
    # A fresh clone has origin's branch but no local one. Take origin's tip first, so this machine continues the same
    # history (a push stays a fast-forward) and a restore sees its rows, instead of starting an unrelated branch.
    local = git(ctx.root, "rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}", check=False).stdout.strip()
    if local:
        return local
    remote = git(ctx.root, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{BRANCH}", check=False).stdout.strip()
    if remote:
        git(ctx.root, "update-ref", f"refs/heads/{BRANCH}", remote, "0" * 40)
    return remote


def _tip_and_text(ctx):
    """The local ade-records tip and its records.jsonl from one cat-file --batch spawn: (tip, text), ("", None) with
    no branch, or None when the output does not parse cleanly (the caller then reads the slow, sure way)."""
    r = git(ctx.root, "cat-file", "--batch", input=f"refs/heads/{BRANCH}\nrefs/heads/{BRANCH}:{FILE}\n", check=False)
    # Sizes are byte counts; the helper decoded with "replace", so a non-UTF-8 byte would shift every later slice.
    out, got = r.stdout.encode(), []
    for want in ("commit", "blob"):
        head, _, out = out.partition(b"\n")
        parts = head.split()
        if len(parts) == 2 and parts[1] == b"missing":
            got.append(None)
            continue
        if len(parts) != 3 or parts[1].decode() != want or not parts[2].isdigit():
            return None
        n = int(parts[2])
        if out[n:n + 1] != b"\n":
            return None
        got.append((parts[0].decode(), out[:n].decode()))
        out = out[n + 1:]
    if r.returncode != 0 or out:
        return None
    return (got[0][0], got[1][1] if got[1] else None) if got[0] else ("", None)


def _branch_text(ctx):
    r = git(ctx.root, "show", f"refs/heads/{BRANCH}:{FILE}", check=False)
    return r.stdout if r.returncode == 0 else None


def _line(r):
    return json.dumps(r, sort_keys=True) + "\n"


def _backup(ctx, f, row=None):
    """Append durable rows to records.jsonl on the ade-records orphan branch with plumbing only: no checkout,
    so the working tree, the index and the current branch never move. It is pushed like any branch."""
    got = _tip_and_text(ctx)
    if got is None or not got[0]:
        # No local branch (a fresh clone takes origin's tip first) or output that did not parse: the two-call read.
        old = _adopt(ctx)
        have = (_branch_text(ctx) or "") if old else ""
    else:
        old, have = got[0], got[1] or ""
    # Every durable row the local file holds that the branch lacks goes up too (a repo that kept records before
    # the branch existed), so a later restore brings back all of them, not just the newest.
    held = set(have.splitlines(True))
    rows = [r for r in (_rows(f) if f else []) if r["type"] in DURABLE and _line(r) not in held] + ([row] if row else [])
    if old and not rows:
        return
    body = have + "".join(map(_line, rows))
    msg = f"{row['type']} {row.get('id', row.get('card', ''))}" if row else f"seed {len(rows)} rows" if rows else "start"
    who = f"{WHO['GIT_COMMITTER_NAME']} <{WHO['GIT_COMMITTER_EMAIL']}> {int(datetime.now(timezone.utc).timestamp())} +0000"
    # One fast-import stream builds blob, tree and commit and moves the ref. Without --force it refuses a
    # non-fast-forward, so a ref another writer moved since `old` is still refused (as update-ref's old value was).
    stream = (f"commit refs/heads/{BRANCH}\ncommitter {who}\ndata {len(msg.encode())}\n{msg}\n"
              + (f"from {old}\n" if old else "") + f"M 100644 inline {FILE}\ndata {len(body.encode())}\n{body}\n")
    git(ctx.root, "fast-import", "--quiet", input=stream)


# Ids are minted as a prefix and a number per type; these are the minting types (ARCHITECTURE.md 3.2).
MINTED = {"card": "c", "decision": "d", "observation": "o"}


def _renumber(theirs, mine, held_ids):
    """Ids this machine minted that the other machine minted too (two c5s, written between pushes). Theirs were
    pushed first and keep the id; ours move to the next free number. Only our unpushed rows are touched, so nothing
    another machine has seen is renamed. Returns {(type, old): new}."""
    taken = {(r["type"], r["id"]) for r in theirs + mine if r["type"] in MINTED and "id" in r}
    moved = {}
    for r in mine:
        t, rid = r["type"], r.get("id")
        numbered = isinstance(rid, str) and rid[:1] == MINTED.get(t) and rid[1:].isdigit()  # not "residuals"
        if numbered and (t, rid) in {(x["type"], x.get("id")) for x in theirs} and (t, rid) not in held_ids:
            if (t, rid) not in moved:
                n = 1 + max((int(i[1:]) for tt, i in taken if tt == t and i[1:].isdigit()), default=0)
                moved[(t, rid)] = f"{MINTED[t]}{n}"
                taken.add((t, moved[(t, rid)]))
    return moved


def _rename(row, moved):
    row = dict(row)
    if (row["type"], row.get("id")) in moved:
        row["id"] = moved[(row["type"], row["id"])]
    if ("card", row.get("card")) in moved:
        row["card"] = moved[("card", row["card"])]
    return row


def reconcile(ctx, theirs, base=None):
    """Fold another machine's ade-records tip into ours: rows from both, theirs first, as a commit with both tips as
    parents. Neither history is rewritten, so the push that follows is a fast-forward. An id both machines minted
    stays theirs and ours is renumbered, in the branch and in the local file; returns {old card id: new}."""
    with host.lock(_path(ctx).with_suffix(".lock")):
        ours = _adopt(ctx)
        if ours and git(ctx.root, "merge-base", "--is-ancestor", theirs, ours, check=False).returncode == 0:
            return {}  # we already hold everything they have
        if not ours or git(ctx.root, "merge-base", "--is-ancestor", ours, theirs, check=False).returncode == 0:
            git(ctx.root, "update-ref", f"refs/heads/{BRANCH}", theirs, ours or "0" * 40)
            _pull_local(ctx, git(ctx.root, "show", f"{theirs}:{FILE}", check=False).stdout, {})
            return {}
        have = git(ctx.root, "show", f"{theirs}:{FILE}", check=False).stdout
        held = set(have.splitlines(True))
        # What both tips already shared (their merge base) was seen on both machines: never renumbered.
        common = git(ctx.root, "merge-base", ours, theirs, check=False).stdout.strip()
        shared = git(ctx.root, "show", f"{common}:{FILE}", check=False).stdout if common else ""
        held_ids = {(r["type"], r.get("id")) for r in map(json.loads, filter(str.strip, shared.splitlines()))}
        mine = [json.loads(ln) for ln in (_branch_text(ctx) or "").splitlines(True) if ln.strip() and ln not in held]
        moved = _renumber(theirs_rows := [json.loads(ln) for ln in held if ln.strip()], mine, held_ids)
        text = have + "".join(_line(_rename(r, moved)) for r in mine)
        # Local-only note of each card moved, so an old id can say it now names the other machine's card.
        title = lambda rows, i: next((r["title"] for r in rows if r.get("id") == i and r.get("title")), "")
        notes = [{"type": "renumber", "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "from": old,
                  "to": new, "title": title(mine, old), "theirs": title(theirs_rows, old)}
                 for (t, old), new in moved.items() if t == "card"]
        blob = git(ctx.root, "hash-object", "-w", "--stdin", input=text)
        tree = git(ctx.root, "mktree", input=f"100644 blob {blob}\t{FILE}\n")
        sha = git(ctx.root, "commit-tree", tree, "-p", ours, "-p", theirs, "-m", "merge records from another machine",
                  env=WHO)
        git(ctx.root, "update-ref", f"refs/heads/{BRANCH}", sha, ours)
        _pull_local(ctx, text, moved, notes)
        return {old: new for (t, old), new in moved.items() if t == "card"}


def _pull_local(ctx, text, moved, notes=()):
    # The local file follows the branch: our rows renumbered as the branch was, the other machine's rows added.
    p = _path(ctx)
    if not p.is_file():
        return
    with host.opentext(p) as f:
        rows = [_rename(r, moved) for r in _rows(f)]
    have = {_line(r) for r in rows}
    rows += [r for r in map(json.loads, filter(str.strip, text.splitlines())) if _line(r) not in have]
    host.write(p, "".join(map(_line, rows + list(notes))))


def seed(ctx):
    # `ade init` calls this so the branch exists from the first install, ready to push with the repo: empty, or
    # carrying the durable rows already on disk that it lacks.
    p = _path(ctx)
    if not (ctx.root / ".git").exists():
        return
    if not p.is_file():
        return _backup(ctx, None)
    with host.lock(p.with_suffix(".lock")), host.opentext(p) as f:
        _backup(ctx, f)


def prune(ctx, days=30):
    # Moves non-durable rows older than DAYS (a row with no readable date stays) to records-archive.jsonl; returns how many.
    p = _path(ctx)
    if not p.is_file():
        return 0
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    def old(line):
        try:
            r = json.loads(line)
            return r["type"] not in DURABLE and str(r["at"]) < cut
        except (ValueError, KeyError, TypeError):
            return False
    with host.lock(p.with_suffix(".lock")):
        lines = host.read(p).splitlines(True)
        keep = [x for x in lines if not old(x)]
        if len(keep) < len(lines):
            gone = [x if x.endswith("\n") else x + "\n" for x in lines if old(x)]
            with open(_archive(ctx), "a", encoding="utf-8") as a:  # first, so a failure loses nothing
                a.write("".join(gone))
            host.write(p, "".join(keep))
        return len(lines) - len(keep)


def _archive(ctx):
    return ctx.state / "records-archive.jsonl"


def fetch(ctx):
    # Pull the remote's records first (a card another machine pushed minutes ago; a shallow or single-branch copy has
    # no such ref). No remote, or no network, just skips.
    name = ctx.cfg.get("git", {}).get("remote", "origin")
    try:
        git(ctx.root, "fetch", "-q", name, f"+refs/heads/{BRANCH}:refs/remotes/{name}/{BRANCH}", check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


_fetched = set()


def _restore(ctx, p):
    # The local file is gone (a dropped drive, a fresh clone): durable rows come back from the branch.
    if not p.exists() and (ctx.root / ".git").exists():
        if ctx.root not in _fetched:
            _fetched.add(ctx.root)
            fetch(ctx)
        _adopt(ctx)
        text = _branch_text(ctx)
        if text:
            p.parent.mkdir(parents=True, exist_ok=True)
            host.write(p, text)


def _rows(f):
    f.seek(0)
    rows, bad = [], []
    for n, line in enumerate(f, 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            bad.append(n)
    if bad:
        print(f"ade: skipped unparsable line {', '.join(map(str, bad))} of {getattr(f, 'name', FILE)}; "
              f"the durable rows are on {BRANCH}; remove the line", file=sys.stderr)
    return rows


def append(ctx, rtype, fields, prefix=None, guard=None):
    # The id is assigned under the same lock as the write so two verbs never mint the same one.
    p = _path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    with host.lock(p.with_suffix(".lock")):
        _restore(ctx, p)
        if guard:
            guard()  # a refusal here leaves the records untouched
        with host.opentext(p, "a+") as f:
            return _append(ctx, f, rtype, fields, prefix)


def _append(ctx, f, rtype, fields, prefix):
    row = {"type": rtype, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if prefix:
        # One past the highest number in use, so a gap or a partial restore can never hand out an id twice.
        # The other machines' rows as last fetched count too, which narrows the window for two machines' c5s;
        # reconcile settles any that still happen.
        seen = _rows(f)
        if (ctx.root / ".git").exists():  # a failed show prints nothing to stdout
            remote = ctx.cfg.get("git", {}).get("remote", "origin")
            seen += [json.loads(x) for x in git(ctx.root, "show", f"refs/remotes/{remote}/{BRANCH}:{FILE}", check=False).stdout.splitlines() if x.strip()]
        nums = [int(r["id"][len(prefix):]) for r in seen if r["type"] == rtype
                and str(r.get("id", "")).startswith(prefix) and r["id"][len(prefix):].isdigit()]
        row["id"] = f"{prefix}{max(nums, default=0) + 1}"
    row.update(fields)
    if rtype == "usage" and os.environ.get("ADE_FLIGHT_ID"):  # the conduct run that made the call
        row["flight"] = os.environ["ADE_FLIGHT_ID"]
    # The branch first: if git refuses, the local file has not moved either, so the two never disagree.
    # A lane (ADE_ROLE set) writes the local file only; the next append or land outside a lane backs up what the branch lacks.
    if rtype in DURABLE and (ctx.root / ".git").exists() and not os.environ.get("ADE_ROLE"):
        _backup(ctx, f, row)
    f.seek(0, 2)
    f.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def fired(ctx, r, card=None):
    # One durable row each time the kit says no, so a code that never fires, or fires on work that lands anyway,
    # shows in `ade budget --refusals` and can be retired.
    # It never raises: a refusal is already being delivered, so a failed backup (git refusing a non-fast-forward)
    # keeps the local row and the next durable append backs it up as it does any row the branch lacks.
    fields = {"code": r.code, **({"card": card} if card else {})}
    try:
        append(ctx, "refusal", fields)
    except Exception:
        try:
            p = _path(ctx)
            p.parent.mkdir(parents=True, exist_ok=True)
            with host.lock(p.with_suffix(".lock")), host.opentext(p, "a+") as f:
                f.write(_line({"type": "refusal", "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}))
        except Exception:
            pass


def read(ctx, rtype=None, archive=False):
    # archive=True puts the rows `ade init` moved aside (older than 30 days) before the live ones; hooks and the loop leave it off.
    p = _path(ctx)
    _restore(ctx, p)
    rows = []
    if archive and _archive(ctx).is_file():
        with host.opentext(_archive(ctx)) as f:
            rows = _rows(f)
    if p.is_file():
        with host.opentext(p) as f:
            rows += _rows(f)
    return [r for r in rows if rtype is None or r["type"] == rtype]


def today():
    return datetime.now(timezone.utc).date()


def merged(ctx, rtype, on=None):
    # Later rows for the same id update earlier ones, so a status change is one appended row.
    out = {}
    for r in read(ctx, rtype):
        row = out.setdefault(r["id"], {})
        row.update(r)
        if rtype == "card" and r.get("status") == "closed" and r.get("retired"):
            row.pop("due", None)  # a hand close retires a recurring card for good
        elif rtype == "card" and r.get("status") in ("landed", "closed") and row.get("every"):
            row["due"] = (date.fromisoformat(r["at"][:10]) + timedelta(days=row["every"])).isoformat()
    for c in out.values():
        # A recurring card rests as landed until its due date (its last landing plus `every` days, or the date a
        # snooze row set), then it is open again; nothing outside ade runs to wake it, the next read does.
        if rtype == "card" and c.get("due"):
            c["status"] = "open" if (on or today()) >= date.fromisoformat(c["due"]) else ("closed" if c["status"] == "closed" else "landed")
    return out
