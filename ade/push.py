# id: ade.push
# what: push trunk, ade-records and the configured tags to the remote: fast-forward only, never forced, never deleted (one exception: prune_merged)
# cmd: -
# reads: ade.toml [git], local refs, the remote
# writes: the remote's trunk, ade-records and tags matching [git] push_tags; ade-records (a merge when two machines wrote)
from ade import records
from ade.config import matches
from ade.gitx import git
from ade.refuse import refuse

# No prompt can be answered from a hook or a lane, so git fails at once instead of waiting on a password.
QUIET = {"GIT_TERMINAL_PROMPT": "0"}


def remote(ctx):
    name = ctx.cfg["git"]["remote"]
    return name if name in git(ctx.root, "remote", check=False).stdout.split() else None


def refs(ctx, trunk=True):
    """The only refs ade ever pushes: trunk, the records branch, and tags matching [git] push_tags. Each refspec
    names the same ref on both sides with no `+`, so git itself refuses anything but a fast-forward or a new ref,
    and there is no `:dst` form, so nothing on the remote is ever deleted here (the one exception is prune_merged below). Lane branches never leave the machine.
    With trunk=False (a protected trunk, [git] push = "pr") neither trunk nor its mirrors are named."""
    local = set(git(ctx.root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags").splitlines())
    heads = [f"refs/heads/{b}" for b in ((ctx.cfg["git"]["main"] if trunk else None), records.BRANCH) if b and f"refs/heads/{b}" in local]
    tags = sorted(r[len("refs/tags/"):] for r in local if r.startswith("refs/tags/"))
    mirrors = [] if not trunk or on_github(ctx.root, ctx.cfg["git"]["remote"]) else \
        [f"refs/heads/{s}:refs/heads/{d}" for s, d in mirror_pairs(ctx.cfg) if f"refs/heads/{s}" in local]
    return heads + [f"refs/tags/{t}" for t in tags if matches(t, ctx.cfg["git"]["push_tags"])] + mirrors


def mirror_pairs(cfg):
    # "master:main" -> ("master", "main"); a `+` (force) or an empty side (a delete) is never honoured.
    pairs = [m.lstrip("+").partition(":") for m in cfg["git"].get("mirror", [])]
    return [(s, d) for s, _, d in pairs if s and d and s != d]


def on_github(root, name):
    # GitHub runs the mirror itself (ade-mirror.yml), so no machine pushes the mirrored branch.
    url = git(root, "config", "--get", f"remote.{name}.url", check=False).stdout.strip()
    return "github.com" in url


def _send(ctx, name, want, dry):
    r = git(ctx.root, "push", "--porcelain", *(["--dry-run"] if dry else []), name, *[x if ":" in x else f"{x}:{x}" for x in want],
            check=False, env=QUIET)
    # Porcelain: one `flag<TAB>src:dst<TAB>summary` line per ref; `!` is rejected, `=` up to date, `*` new.
    rows = [(dst.split(":")[-1], flag, summary) for flag, dst, summary in (ln.split("\t") for ln in r.stdout.splitlines() if ln.count("\t") == 2)]
    if r.returncode and not rows:
        last = (r.stderr.strip() or "no output").splitlines()[-1]
        refuse("push.failed", f"git could not push to {name}: {last}",
               f"check `git remote -v` and that this machine can reach it (`git ls-remote {name}`), then `ade push`")
    return rows


def _reconcile(ctx, name):
    # Another machine pushed records first. Merge its tip in (a commit with two parents, never a rewrite), so no row
    # is lost and the next push is a fast-forward.
    git(ctx.root, "fetch", "-q", name, f"refs/heads/{records.BRANCH}", env=QUIET)
    moved = records.reconcile(ctx, git(ctx.root, "rev-parse", "FETCH_HEAD"))
    from ade.stages import lane  # here, not at the top: stages import this module to push at land
    for old, new in moved.items():
        lane.rename(ctx, old, new)
        print(f"card {old} from this machine is now {new}: the other machine had pushed its own {old} first")
    return moved


def push(ctx, dry=False, trunk=True):
    """Push what refs() names; return (ref, flag, summary) rows, or None when there is no remote to push to."""
    name = remote(ctx)
    if name is None:
        return None
    rows = _send(ctx, name, refs(ctx, trunk), dry)
    if not dry and any(f == "!" and d == f"refs/heads/{records.BRANCH}" for d, f, _ in rows):
        ctx.renumbered.update(_reconcile(ctx, name))
        again = {d: (d, f, s) for d, f, s in _send(ctx, name, [f"refs/heads/{records.BRANCH}"], dry)}
        rows = [again.get(d, (d, f, s)) for d, f, s in rows]
    bad = [(d, s) for d, f, s in rows if f == "!"]
    if bad:
        main = f"refs/heads/{ctx.cfg['git']['main']}"
        cure = (f"git pull --no-rebase {name} {ctx.cfg['git']['main']} (a merge, never a rebase), test, then `ade push`"
                if any(d == main for d, _ in bad) else "a pushed tag is never moved: tag the commit under a new name")
        refuse("push.rejected", f"{name} refused {', '.join(f'{d} ({s})' for d, s in bad)}; nothing was forced", cure)
    return rows


def prune_merged(ctx, ref="FETCH_HEAD"):
    """The one deletion ade makes: under [git] push = "pr", after trunk was fetched (ref), every remote ade/* branch whose
    tip is an ancestor of it goes in one push, each with --force-with-lease=<ref>:<tip checked> so a branch moved since
    is kept. A tip not present locally, or not contained in trunk (squash-merged, open), is kept; a refusal only prints.
    [git] prune_merged = false turns it off."""
    name = remote(ctx)
    if name is None or not ctx.cfg["git"].get("prune_merged", True):
        return []
    out = git(ctx.root, "ls-remote", "--heads", name, "refs/heads/ade/*", check=False, env=QUIET).stdout
    gone = []
    for line in out.splitlines():
        tip, _, head = line.partition("\t")
        if tip and head and git(ctx.root, "cat-file", "-e", f"{tip}^{{commit}}", check=False).returncode == 0 \
                and git(ctx.root, "merge-base", "--is-ancestor", tip, ref, check=False).returncode == 0:
            gone.append((head, tip))
    if gone:
        r = git(ctx.root, "push", "-q", *[f"--force-with-lease={h}:{t}" for h, t in gone], name, *[f":{h}" for h, _ in gone],
                check=False, env=QUIET)
        if r.returncode:
            print(f"{name} refused to delete merged branches ({(r.stderr.strip() or 'no output').splitlines()[-1]}); kept")
            return []
    return [h for h, _ in gone]


def render(rows, name, dry=False):
    if rows is None:
        return "no remote to push to: nothing pushed"
    # A dry run's rows are what the remote would accept, so they say "would", never "pushed".
    word = {"=": "up to date", "*": "would add" if dry else "new", " ": "would push" if dry else "pushed"}
    return "\n".join(f"{word.get(f, s):<10} {d}" for d, f, s in rows) or f"nothing to push to {name}"
