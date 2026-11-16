# id: ade.verbs.push
# what: push trunk, ade-records and checkpoint tags to the remote (under [git] push = "pr", only ade-records and tags, never trunk or its mirrors); fast-forward only, never forced; waits for .ade/land.lock (a landing in progress) first; --dry-run asks the remote without pushing
# cmd: ade push [--dry-run]
# reads: ade.toml [git], local refs
# writes: the remote's trunk (not under push = "pr"), ade-records and tags matching [git] push_tags
from ade import config, host, push


def add(p):
    p.description = 'push trunk, ade-records and tags; with [git] push = "pr" only ade-records and tags, never trunk'
    p.add_argument("--dry-run", action="store_true", help="show what would be pushed; push nothing")


def run(args, cwd):
    ctx = config.load(cwd)
    ctx.state.mkdir(parents=True, exist_ok=True)
    # Wait for a landing in progress (land holds this lock through its checks and any rollback).
    with host.lock(ctx.state / "land.lock", "a landing is in progress; waiting for it to finish"):
        rows = push.push(ctx, args.dry_run, trunk=ctx.cfg["git"].get("push") != "pr")
    print(push.render(rows, ctx.cfg["git"]["remote"], args.dry_run))
