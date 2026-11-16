# id: ade.stages.lane
# what: lane stage: a git worktree per card under $ADE_WORK on branch ade/<card>
# cmd: -
# reads: lane worktree
# writes: $ADE_WORK lanes (with this machine's .claude/settings.local.json and [lanes] share links), ade/<card> branch,
#   an ade block in .git/info/exclude
import os
from pathlib import Path

from ade import host
from ade.gitx import git
from ade.install import LOCAL
from ade.refuse import refuse
from ade.stages import Result, branch, lane_path, main, moved, premise


def _hooks(ctx, lp):
    # A worktree checks out tracked files only; the hooks live in the gitignored local settings, so they go too,
    # on every pass, so a lane made before an `ade init` rerun still runs this machine's current hooks.
    local = ctx.root / LOCAL
    if local.is_file():
        (lp / LOCAL).parent.mkdir(exist_ok=True)
        host.write(lp / LOCAL, host.read(local))


MARK = "# ade: the [lanes] share links in lanes; every lane pass rewrites this block, `ade uninstall` removes it"
END = "# ade: end of the [lanes] share links"


def _exclude(ctx, names):
    # A link is not a folder to git, so `node_modules/` in .gitignore misses it; the repo's own exclude file
    # (local, never committed, shared by every worktree) names each link so no lane commits one. Only the block
    # between the marks is ade's; lines before or after it are the user's and stay.
    p = Path(git(ctx.root, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    old = host.read(p) if p.is_file() else ""
    head, tail = old, ""
    if MARK in old:
        head, rest = old.split(MARK, 1)
        tail = rest.split(END, 1)[1].lstrip("\n") if END in rest else ""
    head = head.rstrip("\n") + "\n" if head.strip() else ""
    block = MARK + "\n" + "".join(f"/{n}\n" for n in names) + END + "\n" if names else ""
    new = head + block + tail
    if new != old and (p.is_file() or names):
        p.parent.mkdir(exist_ok=True)
        host.write(p, new)


def unexclude(ctx):
    _exclude(ctx, [])  # `ade uninstall`: the block goes, the user's own lines stay


def share(ctx, lp):
    """Link each [lanes] share folder (dependencies such as node_modules or .venv, gitignored so a worktree lacks
    them) from the main checkout into the worktree, so its tests run without a fresh install."""
    names = ctx.cfg.get("lanes", {}).get("share", [])
    if names:
        _exclude(ctx, names)
    for n in names:
        src, dst = ctx.root / n, lp / n
        if src.is_dir() and not dst.exists() and not host.is_link(dst):
            host.link_dir(src, dst)


def unshare(ctx, lp):
    # Before a worktree goes: removing the link first means nothing ever deletes through it into the main checkout.
    for n in ctx.cfg.get("lanes", {}).get("share", []):
        host.unlink_dir(lp / n)


def rename(ctx, old, new):
    # A card renumbered by records.reconcile keeps its work: its branch, its lane and its log follow the new id.
    if git(ctx.root, "rev-parse", "-q", "--verify", f"refs/heads/{branch(old)}", check=False).returncode == 0:
        git(ctx.root, "branch", "-m", branch(old), branch(new))
    lp = lane_path(ctx, old)
    if (lp / ".git").exists():
        git(ctx.root, "worktree", "move", str(lp), str(lane_path(ctx, new)))
    log = ctx.state / "lanes" / f"{old}.log"
    if log.is_file():
        log.replace(log.with_name(f"{new}.log"))


def run(ctx, c):
    lp = lane_path(ctx, c["id"])
    if (lp / ".git").exists():
        if git(lp, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() == main(ctx):
            refuse("lane.refused", f"lane {lp} is on the trunk branch {main(ctx)}, so its work would land on trunk itself",
                   f"git -C {lp} checkout -b {branch(c['id'])}")
        _hooks(ctx, lp)
        share(ctx, lp)
        return Result("advance", f"lane {lp}")
    have = git(ctx.root, "branch", "--list", branch(c["id"]))
    if not have and git(ctx.root, "rev-parse", "-q", "--verify", f"refs/heads/{main(ctx)}", check=False).returncode:
        refuse("lane.refused", f"no local trunk branch {main(ctx)} to start {branch(c['id'])} from",
               f"git branch {main(ctx)} <remote>/{main(ctx)}")
    args = [str(lp), branch(c["id"])] if have else ["-b", branch(c["id"]), str(lp), main(ctx)]
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"):  # Codex sets these in its sandbox
        refuse("lane.sandboxed", "ADE is running inside a Codex sandbox, where git refs under .git and the build engines "
               "(codex app-server, claude) cannot run",
               "run ade loop/conduct from a plain terminal, or start the Codex pilot with --sandbox danger-full-access "
               "(ADE's lanes carry their own guards)")
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        probe = lp.parent / f".ade-probe-{c['id']}"  # an existing folder may still be unwritable
        probe.write_text("")
        probe.unlink()
        git(ctx.root, "worktree", "add", *args)
    except OSError as e:
        refuse("lane.work_unwritable", f"cannot write the lane folder {lp.parent} ({e.strerror or e}); the engine's sandbox "
               "allows writes only inside the workspace",
               f"let the engine write {lp.parent} (Codex: add it to writable_roots under [sandbox_workspace_write] in "
               "~/.codex/config.toml), or set ADE_WORK to a writable folder outside the repo")
    _hooks(ctx, lp)
    share(ctx, lp)
    # The pick: say if main moved under the card's paths since it was written, so the builder rereads them, and if
    # the card names code its files do not hold. Both are notes, never stops; the builder is told the second too.
    notes = [moved(ctx, c)] + [f"{c['id']} names {', '.join(missing)}, not yet in its files"
                               for missing in [premise(ctx, c, lp)] if missing]
    return Result("advance", "; ".join([f"lane {lp}", *filter(None, notes)]))
