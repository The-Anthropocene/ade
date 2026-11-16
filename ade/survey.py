# id: ade.survey
# what: read an existing repo's quirks at first init (trunk, mirror refspecs, CI and deploy triggers, deps, typecheck) and pick safe ade.toml values
# cmd: -
# reads: git config and refs, .github/workflows/*.yml, package.json, .gitignore'd dependency folders
# writes: -
import json
import re

from ade import host, push
from ade.gitx import git

# A step that ships something when it runs. A push that starts one is a deploy, so landings must not make it.
DEPLOY = re.compile(r"wrangler\s+(pages\s+)?(deploy|publish)|vercel\s+(deploy|--prod)|netlify\s+deploy|firebase\s+deploy"
                    r"|fly\s+deploy|gh-pages|aws\s+s3\s+sync|kubectl\s+apply|terraform\s+apply|heroku|npm\s+run\s+deploy",
                    re.I)
DEPS = ["node_modules", ".venv", "venv", "vendor"]


def _push_branches(text):
    """Branches a workflow's `on: push` fires for: a list, ["*"] when push has no branch filter, or [] for none."""
    m = re.search(r"(?m)^on:[ \t]*(.*)$", text)
    if not m:
        return []
    if m.group(1).strip():  # on: push  /  on: [push, pull_request]
        return ["*"] if re.search(r"\bpush\b", m.group(1)) else []
    block = text[m.end():]
    end = re.search(r"(?m)^\S", block)
    block = block[:end.start()] if end else block
    push = re.search(r"(?m)^([ \t]+)push:(.*)$", block)
    if not push:
        return []
    body = block[push.end():]
    nxt = re.search(rf"(?m)^{push.group(1)}\S", body)  # the next key at push's own indent ends its block
    body = push.group(2) + "\n" + (body[:nxt.start()] if nxt else body)
    inline = re.search(r"branches:\s*\[([^\]]*)\]", body)
    if inline:
        return [b.strip().strip("'\"") for b in inline.group(1).split(",") if b.strip()]
    listed = re.search(r"(?m)^[ \t]+branches:\s*\n((?:[ \t]+-.*\n?)+)", body)
    if listed:
        return [x.strip()[1:].strip().strip("'\"") for x in listed.group(1).splitlines() if x.strip()]
    return [] if "tags:" in body else ["*"]


def _fires(branches, name):
    return any(b == name or b in ("*", "**") or (b.endswith("*") and name.startswith(b.rstrip("*"))) for b in branches)


def survey(root, remote="origin"):
    """(findings to print, {ade.toml key: value} chosen). Every choice is the safe one, and each says why."""
    say, pick = [], {}
    here = git(root, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    head = git(root, "symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD", check=False).stdout.strip()
    if here:
        pick["main"] = here
        say.append(f"trunk: {here} (the branch init ran on)")
    if head and here and head != f"{remote}/{here}":
        say.append(f"note: {remote}'s default branch is {head.split('/', 1)[1]}, not {here}; set [git] main if that is the trunk")
    # A mirror kept by remote.<name>.push refspecs: ade names its refspecs, which makes git skip these, so they
    # are copied into ade.toml where every clone sees them. Never with `+` (no force), never a delete.
    mirrors = []
    for spec in git(root, "config", "--get-all", f"remote.{remote}.push", check=False).stdout.split():
        src, _, dst = (x.removeprefix("refs/heads/") for x in spec.lstrip("+").partition(":"))
        if src and dst and src != dst:
            mirrors.append(f"{src}:{dst}")
    if mirrors:
        pick["mirror"] = mirrors
        how = ("GitHub moves it after each push (.github/workflows/ade-mirror.yml), so no machine pushes it"
               if push.on_github(root, remote) else "ade push sends it, fast-forward only")
        say.append(f"mirror: {', '.join(mirrors)} from remote.{remote}.push; {how}")
    pushed = {here} | {m.split(":")[1] for m in mirrors}
    wf, ci = root / ".github" / "workflows", []
    for f in sorted(wf.glob("*.y*ml")) if wf.is_dir() else []:
        text = re.sub(r"(?m)(^|\s)#.*$", "", host.read(f, errors="replace"))  # a comment never deploys
        hit = [b for b in sorted(pushed) if _fires(_push_branches(text), b)]
        ci += [f.name] if here in hit else []
        if hit and DEPLOY.search(text):
            pick["push"] = "manual"
            say.append(f"deploy: pushing {', '.join(hit)} runs {f.name}, which deploys ({DEPLOY.search(text).group(0)}); "
                       "landings will not push, so run `ade push` when you mean to ship (or move deploys off push)")
    if here and wf.is_dir() and not ci:
        say.append(f"note: no workflow in .github/workflows runs on a push to {here}, so CI will not see landings"
                   + ("; a mirrored push starts no workflow on GitHub, so add it to CI's push branches" if mirrors else ""))
    pkg = root / "package.json"
    scripts = json.loads(host.read(pkg)).get("scripts", {}) if pkg.is_file() else {}
    share = [d for d in DEPS if (d != "node_modules" or pkg.is_file()) and _ignored(root, d)]
    if share:
        pick["share"] = share
        say.append(f"deps: lanes link {', '.join(share)} from this checkout")
    if pkg.is_file() and not (root / "node_modules").exists():
        say.append("note: package.json but no node_modules; install once here (npm ci), lanes link it")
    if "typecheck" in scripts:
        pick["always"] = ["npm run typecheck"]
        say.append("tests: `npm run typecheck` runs before every code change's tests")
    if pkg.is_file() and "test" in scripts:
        pick["command"] = "npm test"
        say.append("tests: `npm test` (edit [tests] command to run the graph's selection per file)")
    elif pkg.is_file():
        say.append("note: no npm test script; set [tests] command (for example `npx tsx {test}`)")
    venv = next((d for d in (".venv", "venv") if _ignored(root, d)), None)
    marker = (root / "pytest.ini").is_file() or (root / "conftest.py").is_file() or (
        (root / "pyproject.toml").is_file() and "[tool.pytest.ini_options]" in host.read(root / "pyproject.toml", errors="replace"))
    if (venv or marker) and not pkg.is_file():
        if venv and (root / venv / "bin" / "pytest").is_file():
            pick["command"] = f"{venv}/bin/python -m pytest {{tests}}"
            say.append(f"tests: `{pick['command']}` (the kit Python does not see the project packages)")
        else:
            say.append("tests: the kit Python will not see the project packages or pytest; "
                       "set [tests] command (for example `.venv/bin/python -m pytest {tests}`)")
    return say, pick


def _ignored(root, d):
    return (root / d).is_dir() and git(root, "check-ignore", "-q", d, check=False).returncode == 0


def apply(toml, pick):
    """The template's text with the chosen values in place of its defaults, so the comments stay."""
    lines = {"main": ('main = "main"', lambda v: f'main = "{v}"'),
             "push": ('push = "land"', lambda v: f'push = "{v}"'),
             "mirror": ("mirror = []", lambda v: f"mirror = {json.dumps(v)}"),
             "command": ('command = "{python} -m unittest {tests}"', lambda v: f"command = {json.dumps(v)}"),
             "always": ("always = []", lambda v: f"always = {json.dumps(v)}"),
             "share": ("share = []", lambda v: f"share = {json.dumps(v)}")}
    for key, value in pick.items():
        old, new = lines[key]
        toml = toml.replace(old, new(value), 1)
    return toml
