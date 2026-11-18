# id: ade.verbs.init
# what: start a new project in an empty folder, or install or refresh ADE in a repo from ade.toml; idempotent, --dry-run prints the diff
# cmd: ade init [--starter python|typescript] [--dry-run] [--yes] [--no-commit]
# reads: ade.toml, existing generated files
# writes: git repo and starter files (new project), ade.toml (once), .gitignore, .gitattributes, .claude/, .codex/hooks.json, .opencode/plugin/ade.ts, .pi/extensions/ade.ts, AGENTS.md, graph.json, .ade/kit.json, .ade/template.toml, .ade/news.md
import difflib
import json
import re
import sys
import tomllib
from pathlib import Path

from ade import config, firstrun, guard, host, install, providers, push, records, stages, starters, survey
from ade.gitx import git
from ade.refuse import refuse
from ade.verbs import setup


def add(p):
    p.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    p.add_argument("--starter", choices=starters.names(),
                   help=f"for an empty folder: the starter project to write (default {starters.DEFAULT})")
    p.add_argument("--yes", action="store_true", help="do not open the setup picker on a first ade.toml")
    p.add_argument("--no-commit", action="store_true", help="in an existing repo, leave the written files uncommitted")


def _pick(args, folder):
    # A first ade.toml opens in the setup picker, only when a person is at the terminal.
    if not args.yes and sys.stdin.isatty() and sys.stdout.isatty():
        setup.run(args, folder)
    return config.load_at(folder)


def _empty(folder):
    return not any(p.name != ".git" for p in folder.iterdir())


def _write(root, plan, dry_run, seen=()):
    changed = []
    for rel, new in plan:
        p = root / rel
        old = host.read(p) if p.is_file() else ""
        if old == (new or "") and (new is not None or not p.is_file()):
            continue
        changed.append(rel)
        if dry_run and new and not p.is_file():
            print(f"new  {rel}  {len((new).splitlines())} lines")
        elif dry_run:
            print("".join(difflib.unified_diff(old.splitlines(True), (new or "").splitlines(True), f"a/{rel}", f"b/{rel}")))
        elif new is None:
            p.unlink()
            print(f"removed {rel}")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            host.write(p, new)
            if rel not in seen:
                print(f"wrote {rel}")
    return changed


def _install(root, ctx, dry_run, seen=()):
    # The graph is built from disk, so on a first install it misses the files this same plan writes (the opencode
    # and Pi adapters): a second pass, once they exist, brings it level and leaves every other file as written.
    changed = _write(root, install.plan(ctx), dry_run, seen)
    return changed + ([] if dry_run else [r for r in _write(root, install.plan(ctx), False, (*seen, *changed)) if r not in changed])


def start(folder, starter, dry_run, args):
    files, card = starters.files(starter, folder.name)
    if dry_run:
        print(f"ade init --dry-run: a new {starter} project in {folder}, with {len(files)} starter files:")
        print("\n".join(f"  {rel}" for rel in sorted(files)))
        print("plus the generated settings, agents, AGENTS.md and graph; nothing written")
        return 0
    # git var asks for the identity the way git commit does, so GIT_AUTHOR_*/GIT_COMMITTER_* count as well as user.*.
    for who in ("AUTHOR", "COMMITTER"):
        if git(folder, "var", f"GIT_{who}_IDENT", check=False).returncode != 0:
            refuse("init.refused", "git has no user name or email, so the first commit would fail after the files were written",
                   'run `git config --global user.name "Your Name"` and `git config --global user.email "you@example.com"`, then `ade init`')
    if config.toplevel(folder) is None:
        git(folder, "init", "-q", "-b", "main")
    else:  # a folder where git init already ran keeps that branch as trunk
        here = git(folder, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
        if here:
            line = survey.apply('main = "main"', {"main": here})  # the toml holds only non-default keys
            toml = files["ade.toml"]
            files["ade.toml"] = toml.replace("[git]\n", f"[git]\n{line}\n", 1) if "[git]\n" in toml else f"{toml}\n[git]\n{line}\n"
    wrote = _write(folder, sorted(files.items()), False)
    ctx = _pick(args, folder)
    _install(folder, ctx, False, wrote)
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", f"start {folder.name} with ade ({starter} starter)")
    row = records.append(ctx, "card", {**card, "status": "open", "probed_at": stages.probe(ctx)}, prefix="c")
    print(f"first card {row['id']}: {row['title']}\nnext: ade loop {row['id']}")
    return 0


def _news(ctx, old):
    """On an upgrade, .ade/news.md: the release-notes sections newer than OLD (Unreleased too), or a pointer."""
    ver = lambda t: tuple(int(x) for x in re.findall(r"\d+", t.split(" ")[0])[:3])
    notes = Path(install.__file__).resolve().parent.parent / "site" / "docs" / "08-release-notes.md"
    if notes.is_file():
        parts = re.split(r"(?m)^(?=## )", host.read(notes))[1:]
        body = "".join(p for p in parts if p.startswith("## Unreleased") or (
            re.match(r"## \d", p) and ver(p[3:]) > ver(old))) or "No newer release notes.\n"
    else:
        body = "Release notes: https://atlas-ade.dev/docs/release-notes/\n"
    host.write(ctx.state / "news.md", f"<!-- since {old} -->\n{body}")


def _say_push(ctx):
    # Says when the records branch has not left this machine yet; `ade push` (or the next landing) carries it.
    name = push.remote(ctx)
    have = git(ctx.root, "rev-parse", "-q", "--verify", f"refs/heads/{records.BRANCH}", check=False).returncode == 0
    if have and name and git(ctx.root, "rev-parse", "-q", "--verify",
                             f"refs/remotes/{name}/{records.BRANCH}", check=False).returncode:
        print(f"records branch {records.BRANCH} is only on this machine: ade push")


def _leaves(t, pre=""):
    for k, v in t.items():
        yield from _leaves(v, f"{pre}{k}.") if isinstance(v, dict) else [(pre + k, v)]


def _old_defaults(ctx):
    """Warn (never write) for each ade.toml value that equals the saved old template's but not the running kit's."""
    try:
        old = dict(_leaves(tomllib.loads(host.read(ctx.state / "template.toml"))))
    except (OSError, ValueError):
        return  # no saved copy: a first run, or a project installed before the copy was kept
    new, mine = dict(_leaves(tomllib.loads(host.read(install.TEMPLATE)))), dict(_leaves(ctx.cfg))
    try:
        written = dict(_leaves(tomllib.loads(host.read(ctx.root / "ade.toml"))))
    except (OSError, ValueError):
        written = mine
    one = lambda x: " ".join(json.dumps(x).split())[:60]  # as TOML reads it: true, "text", [1, 2]
    for k, v in old.items():
        if k not in written and v != new.get(k, v):
            print(f"{k} default changed {one(v)} -> {one(new[k])}; add the line to ade.toml to keep the old one")
        elif k in mine and mine[k] == v != new.get(k, v):
            print(f"{k} is the old default {one(v)}; the new default is {one(new[k])} (delete the line to take it)")


def run(args, cwd):
    folder = Path(cwd).resolve()
    root = config.toplevel(folder)
    if (root is None or root == folder) and _empty(folder):
        return start(folder, args.starter or starters.DEFAULT, args.dry_run, args)
    if args.starter:
        refuse("init.refused", f"--starter writes a new project, and {folder} already has files",
               "run it in an empty folder, or drop --starter to install ADE into this repository")
    if root is None and (folder / ".git").is_dir() and not any((folder / ".git").iterdir()):
        refuse("init.refused", f"{folder}/.git is empty, which is what an agent sandbox (Codex's default workspace-write) mounts read-only",
               "approve `git init` and `ade init` for the agent, or run them yourself in a normal shell")
    if root is None:
        refuse("init.refused", f"{cwd} is not inside a git repository",
               "run `git init -b main` first, then `ade init`; or run `ade init` in an empty folder to start a project")
    ctx = config.load_at(root)
    made = []
    if not (root / "ade.toml").is_file():
        # An existing repo's quirks are read once, into the first ade.toml; each choice is printed with its reason.
        found, pick = survey.survey(root, ctx.cfg["git"]["remote"])
        print("\n".join(["ade init: this repository, as found"] + [f"  {x}" for x in found]))
        if not args.dry_run:
            host.write(root / "ade.toml", firstrun.with_defaults(survey.apply(host.read(install.TEMPLATE), pick)))
            print("wrote ade.toml")
            made = ["ade.toml"]
            ctx = _pick(args, root)
    engines = install.engines(ctx)
    if firstrun.is_root() and (root / "ade.toml").is_file() and ctx.cfg["permissions"]["lane_mode"] == "bypassPermissions":
        print('ade.toml [permissions] lane_mode = "bypassPermissions": Claude Code refuses it as root; change it to "auto"')
    if "codex" in engines:
        print("Codex lanes carry ADE's hooks as -c flags; accept them once per machine ('Trust all and continue') by opening Codex with:\n  "
              + install.codex_open_command() + "\nuntil then Codex runs none of them; after an ADE update that changes a hook, accept again")
        if install.ade_cmd() != "ade":
            print("Codex hooks name this kit by its full path, so an ADE update in a new folder needs them accepted again; "
                  "run ade link once so they read ade hook ... and survive updates")
        miss = install.codex_untrusted(ctx)
        if miss and miss["sessions"]:
            print(f"Codex has not yet trusted {len(miss['sessions'])} of ADE's {len(install.HOOKS)} hooks in this folder's .codex/hooks.json "
                  "(your own Codex sessions): open Codex here and accept them when it asks")
        if miss and miss["lanes"]:
            print(f"Codex has not yet accepted {len(miss['lanes'])} of ADE's {len(install.HOOKS)} lane hooks (session flags): "
                  "run the codex command above and choose 'Trust all and continue'")
        if miss and miss["different"]:
            print(f"Codex holds {len(miss['different'])} of ADE's hook keys trusted for a different hook: accept again "
                  "with the codex command above (or open Codex here for your own sessions)")
    if "codex" in engines and any(g.startswith("tracked:") for g in ctx.cfg["guards"]["secret_ok"]):
        print("secret_ok tracked: globs do not reach Codex lanes: its profile cannot express an allow, so they keep the plain deny")
    hid = sorted(p for p in guard.tracked(str(root)) if guard.secret(ctx.cfg, str(root / p), str(root), str(root)))
    if hid:
        print(f"[guards] secrets hides {len(hid)} tracked file(s): {', '.join(hid[:5])}{' ...' if len(hid) > 5 else ''}; "
              "agents and reviewers cannot read them; allow them with [guards] secret_ok, e.g. 'tracked:GLOB' (allowed only while git tracks the file)")
    lane_mode, cx = ctx.cfg["permissions"]["lane_mode"], ctx.cfg["providers"].get("codex", {})
    cx_mode = cx.get("modes", {}).get(lane_mode, cx.get("modes", {}).get("default", []))
    if "codex" in engines and "{deny_reads}" not in cx_mode:  # an ade.toml from before every mode took the profile
        print(f"codex lanes run in {lane_mode} without {{deny_reads}}: [guards] secrets are not read-denied there "
              "(set [providers.codex] modes = { default = [\"{deny_reads}\"] } to guard them)")
    for n in sorted(engines & set(ctx.cfg["providers"])):
        if ctx.cfg["providers"][n].get("sandbox") == "os" and not providers.os_guard(ctx, root):
            print(f"{n} lanes are not read-guarded here: no sandbox-exec (macOS) or bwrap (Linux) on PATH")
    if "claude" in engines:  # an ade.toml from before the template scrubbed more of the host session's environment
        have = ctx.cfg["providers"].get("claude", {}).get("scrub_env", [])
        lack = [e for e in tomllib.loads(host.read(install.TEMPLATE))["providers"]["claude"]["scrub_env"] if e not in have]
        if lack:
            print(f"[providers.claude] scrub_env lacks {', '.join(lack)}: claude lanes will inherit them from the host session "
                  "(add them to scrub_env, or delete the scrub_env line to take the template's)")
    md = root / "CLAUDE.md"  # Claude Code reads AGENTS.md only when there is no CLAUDE.md; never edited here
    if "claude" in engines and md.is_file() and not re.search(r"(?m)^\s*@AGENTS\.md\s*$", host.read(md)):
        print("Claude Code reads CLAUDE.md instead of AGENTS.md here, so add the line `@AGENTS.md` to CLAUDE.md for Claude seats to see ADE's block")
    f = root / ".claude" / "settings.json"
    gone = install.removed(ctx, json.loads(host.read(f))) if f.is_file() else []
    if gone:
        print(f"ade init: removing {len(gone)} rule(s) from .claude/settings.json that ade.toml no longer has:")
        for kind, rule in gone:
            print(f"  {kind:<5} {rule}")
        print("add them to [permissions.allow] or [permissions.deny] to keep them")
    _old_defaults(ctx)
    changed = _install(root, ctx, args.dry_run)
    if not args.dry_run:
        records.seed(ctx)
        if dropped := records.prune(ctx):
            print(f"ade init: archived {dropped} non-durable rows older than 30 days from .ade/records.jsonl to .ade/records-archive.jsonl")
        ctx.state.mkdir(exist_ok=True)
        host.write(ctx.state / "template.toml", host.read(install.TEMPLATE))
        try:
            old = json.loads(host.read(ctx.state / "kit.json"))["kit"]
        except (OSError, ValueError, KeyError, TypeError):
            old = None  # a first install: no news
        if old and old != install.kit():
            _news(ctx, old)
        host.write(ctx.state / "kit.json", json.dumps({"kit": install.kit()}) + "\n")
        _say_push(ctx)
    if (changed or made) and not args.dry_run:
        print(f"ADE's hooks and settings load only for an agent session started in {root}; an agent started elsewhere should be restarted from this folder.")
    if not changed and not made:
        print("ade init: up to date")
    elif args.dry_run:
        print(f"ade init --dry-run: {len(changed)} file(s) would change; nothing written")
    elif args.no_commit:
        print("next: review and commit these files, then `ade card add` and `ade loop <card>`")
    else:
        # Exactly the files written, by path, so the person's other changes stay unstaged and out of the commit.
        # Ignored ones (settings.local.json) are written but never committed; git add refuses them by name.
        ignored = set(git(root, "check-ignore", "--", *made, *changed, check=False).stdout.splitlines())
        paths = [p for p in made + changed if p not in ignored]
        if paths:
            git(root, "add", "--", *paths)
            # Files rewritten identical to HEAD (uninstall then init) leave nothing to commit.
            if git(root, "diff", "--cached", "--quiet", "--", *paths, check=False).returncode == 1:
                git(root, "commit", "-q", "-m", "install ade", "--", *paths)
                print("committed: install ade")
        print("next: `ade card add` and `ade loop <card>`")
