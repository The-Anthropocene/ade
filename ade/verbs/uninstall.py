# id: ade.verbs.uninstall
# what: remove everything `ade init` generated (hooks, rules, agents, AGENTS.md block, graph); keep ade.toml and records
# cmd: ade uninstall [--dry-run]
# reads: ade.toml, the generated files
# writes: .git/info/exclude (ade's block), lane share links, .claude/settings.json, .claude/settings.local.json, .codex/hooks.json, .claude/agents/ade-*.md, .claude/skills/ade/SKILL.md, .agents/skills/ade/SKILL.md, .claude/skills/ade-flight-band/*, .opencode/plugin/ade.ts, .pi/extensions/ade.ts, AGENTS.md, .gitattributes, graph.json
from ade import config, install
from ade.stages import lane, lane_path
from ade.verbs.init import _write


def add(p):
    p.add_argument("--dry-run", action="store_true", help="print what would change, remove nothing")


def run(args, cwd):
    ctx = config.load(cwd)
    changes = install.removal(ctx)
    keep = [(rel, new) for rel, new in changes if new is not None]
    gone = [rel for rel, new in changes if new is None]
    _write(ctx.root, keep, args.dry_run)
    for rel in gone:
        print(f"{'would remove' if args.dry_run else 'removed'} {rel}")
        if not args.dry_run:
            f = ctx.root / rel
            f.unlink()
            for d in f.parents:  # prune folders the removal emptied, never the root or a folder holding anything
                if d == ctx.root or any(d.iterdir()):
                    break
                d.rmdir()
    lanes = [p for p in lane_path(ctx, "x").parent.glob("*") if (p / ".git").exists()]
    if not args.dry_run:
        for p in lanes:
            lane.unshare(ctx, p)  # the shared links go first, so removing a lane by hand never reaches main's folders
        lane.unexclude(ctx)
    if lanes:
        print(f"open lanes stay ({len(lanes)}); remove each with `git worktree remove <path>`: "
              + ", ".join(str(p) for p in lanes))
    print(("nothing to remove; " if not changes else "") + "kept ade.toml, .ade/ (records) and the ade-records branch: "
          "your config and history. Delete them by hand to forget everything; `ade init` reinstalls.")
