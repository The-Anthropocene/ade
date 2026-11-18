# id: ade.providers
# what: provider adapters (claude, codex, opencode, pi from config argv; fake built in) behind one call
# cmd: -
# reads: ade.toml [report] seats (a 'config:' line per call), [roles.<role>] system_prompt (a repo-relative file) and subagents, [providers] (default_effort, auto_compact_token_limit and context_window for codex, sandbox = "os" wraps a call in sandbox-exec or bwrap), [roles] (a card's [roles.<role>.profiles.<name>] over its role, picked by the card or [profiles] select), [permissions] allow/deny (deny with the Read and Grep rules of [guards] secrets) and [guards] secrets (claude and codex lanes, and the pass_env check), each provider's scrub_env (an engine with none takes the claude list plus lib/agent MARKERS), pass_env and secrets files (into its own call's environment only), Claude Code's user, project and local settings files (`ade config --seats`)
# writes: lane working tree (via the provider), .ade/records.jsonl (usage rows), .ade/lanes/<card>.log, .settings.json and .<role>.prompt.md (Pi reads its prompt as @file)
import fnmatch
import functools
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from ade import config, guard, host, install, records, telemetry
from ade.refuse import refuse

STAGE = {"builder": "build", "reviewer": "review"}


BIG_PROMPT = 100_000  # bytes, well under ARG_MAX


def engine(ctx, role, name=None, card=None):
    # ADE_PROVIDER forces every role, so a rehearsal can never spend usage by accident. `name` is a caller's
    # choice of provider for this call (the opposite-engine review); else the role's, as the card's profile sets it.
    return os.environ.get("ADE_PROVIDER") or name or role_cfg(ctx, role, card)["provider"]


def profiles(ctx):
    """Every profile name a role defines ([roles.<role>.profiles.<name>]), sorted; ADE ships none."""
    return sorted({n for r in ctx.cfg["roles"].values() for n in r.get("profiles", {})})


def profile(ctx, card):
    """The profile in force for a card: its own (`ade card add --profile`), else the first [profiles] select glob
    (in the order written) that matches one of its paths; None when neither names one."""
    if card.get("profile"):
        return card["profile"]
    sel = ctx.cfg.get("profiles", {}).get("select", {})
    return next((n for g, n in sel.items() if any(fnmatch.fnmatchcase(p, g) for p in card.get("paths", []))), None)


def role_cfg(ctx, role, card=None):
    """[roles.<role>] with the card's profile merged over it: voice (a file path) and provider replace, models and
    effort merge per engine. A role that does not define the profile is itself. env and test policy are not profile keys."""
    r = ctx.cfg["roles"][role]
    over = r.get("profiles", {}).get(profile(ctx, card), {}) if card else {}
    out = {**r, **({"provider": over["provider"]} if "provider" in over else {}), **{k: {**r.get(k, {}), **over[k]} if k != "voice" else over[k]
                   for k in ("voice", "models", "effort", "args") if k in over}}
    if "system_prompt" in over and "system_prompt" not in r:  # the role's own key wins over its profile's
        out["system_prompt"] = over["system_prompt"]
    return out


def system_prompt_file(ctx, role, card=None):
    """The role's system_prompt as a readable repo-relative file, or None; a missing or unreadable file refuses."""
    rel = role_cfg(ctx, role, card).get("system_prompt")
    if not rel:
        return None
    p = ctx.root / rel
    try:
        p.read_bytes()
    except OSError as e:
        refuse("provider.setup", f"[roles.{role}] system_prompt {rel!r} cannot be read: {e.strerror}",
               f"create {rel} (repo-relative), or remove system_prompt from [roles.{role}] in ade.toml")
    return p


def log_path(ctx, card_id, role=None, name=None):
    """.ade/lanes/<card>.log; given a role, a header line for the call that is about to write to it."""
    p = ctx.state / "lanes" / f"{card_id}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    if role:
        with host.opentext(p, "a") as f:
            f.write(f"--- {role} via {engine(ctx, role, name)} ---\n")
    return p


def ade_bin():
    """{"ADE_BIN": path} of this kit's ade when it has one (PATH's when it is this kit, else bin/ade on POSIX), else {}."""
    found = shutil.which("ade")
    kit = Path(install.__file__).resolve().parent.parent / "bin" / "ade"
    path = found if found and install._this_kit(found) else str(kit) if os.name != "nt" and os.access(kit, os.X_OK) else ""
    return {"ADE_BIN": path} if path else {}


def _env(spec, name=None):
    # A provider started from inside another session (ade run from Claude Code) would inherit that session's
    # variables and act as it, not as the user's own login; [providers.<name>] scrub_env names what is dropped.
    # pass_env names what a project keeps though scrub_env would drop it (check_pass_env refused any secret's name at load).
    env = {k: v for k, v in os.environ.items() if not _scrubbed(spec, k, name)}
    return {**env, **{k: _secret(k, f) for k, f in spec.get("secrets", {}).items()}}


# Never passable: a key belongs in secrets (a file, this call only), and CLAUDE_CODE_REMOTE / CLAUDE_CODE_ENTRYPOINT
# would swap a lane onto the host's plan login. [guards] secrets globs are added to these at load.
PASS_NEVER = ("ANTHROPIC_API_KEY", "*_KEY", "*_TOKEN", "*_SECRET", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_ENTRYPOINT")


# The one key an engine's lane keeps from the host (unless [providers.<engine>] secrets names it); every other
# *_KEY, *_TOKEN and *_SECRET host variable is dropped for every engine.
AUTH_ENV = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"), "opencode": ("OPENROUTER_API_KEY",)}


def _scrubbed(spec, k, name=None):
    """True when a call drops host variable k: a key, token or secret glob (bar the engine's own auth variable), or a scrub_env glob that pass_env does not keep."""
    if k not in AUTH_ENV.get(name, ()) and any(fnmatch.fnmatchcase(k.upper(), g) for g in PASS_NEVER[1:4]):
        return True
    return k not in spec.get("pass_env", []) and any(fnmatch.fnmatchcase(k, g) for g in spec.get("scrub_env", _shared_scrub()))


@functools.cache
def _shared_scrub():
    """The scrub list of an engine that names none: the template's claude scrub_env plus every other harness's session markers (lib/agent MARKERS)."""
    from ade.lib import agent
    import tomllib
    return tomllib.loads(host.read(install.TEMPLATE))["providers"]["claude"]["scrub_env"] + [n for _, ns in agent.MARKERS for n in ns]


def _never(cfg, n):
    """The PASS_NEVER or [guards] secrets glob that name n matches (case-blind), or None: n names no secret."""
    never = PASS_NEVER + tuple(cfg.get("guards", {}).get("secrets", []))
    return next((g for g in never if fnmatch.fnmatchcase(str(n).lower(), g.lower())), None)


def check_pass_env(cfg):
    """Refuse a [providers.<engine>] pass_env name that matches PASS_NEVER or a [guards] secrets glob (case-blind)."""
    for eng, spec in cfg.get("providers", {}).items():
        for n in spec.get("pass_env", []):
            hit = _never(cfg, n)
            if hit:
                refuse("config.pass_env", f"[providers.{eng}] pass_env names {n}, which matches {hit!r}: a secret or the plan-login swap",
                       f"drop {n} from pass_env; give a key by file with [providers.{eng}] secrets (NAME = a key file path)")


def _secret(var, path):
    # [providers.<name>] secrets maps a variable to a key file outside the repo; the value reaches only this call's
    # environment, never ade's own, a log or a record. The file holds the key alone or one `name=key` line.
    p = Path(path).expanduser()
    if not p.is_file():
        refuse("provider.secret", f"no key file at {p} for {var}", f"put the key in {p}, or drop {var} from secrets")
    line = host.read(p).strip()
    name, eq, rest = line.partition("=")
    # A bare key may end in base64 padding (`...==`), so only a name with a real value after it is a `name=key` line.
    return rest.strip() if eq and name.strip().isidentifier() and rest.strip() and not rest.startswith("=") else line


def fork_args(ctx, role, name=None, card=None):
    """(new, fork): the role's provider's argv pieces that start a session and fork one, each holding {session};
    None when it names no pair (codex, opencode), and the fake always has one so a rehearsal can fork."""
    name = engine(ctx, role, name, card)
    spec = {"new": ["--session-id", "{session}"], "fork": ["--resume", "{session}", "--fork-session"]} \
        if name == "fake" else ctx.cfg["providers"].get(name, {})
    return (spec["new"], spec["fork"]) if spec.get("new") and spec.get("fork") else None


CAPACITY = ("overloaded_error", "529 overloaded", "selected model is at capacity",
            "you've hit your usage limit", "usage limit reached", "hit your limit")  # claude's and codex's own words; the last three are a plan's 5-hour or weekly allowance
CREDIT = "credit balance is too low"  # claude's synthetic answer (model '<synthetic>', 0 tokens) when the key is out of credit


def _glob_regex(glob):
    """One glob as a regex body: * within a name, ** across directories, ? one char; every other character is escaped."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**", i):
            out, i = out + ".*", i + 2
            continue
        c = glob[i]
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return out


def _seatbelt_profile(secrets, secret_ok=(), cwd=None):
    """A macOS seatbelt (SBPL) profile that allows all but reads of every secrets glob. Absolute and ~/ entries are anchored as
    given (~ expanded); a bare glob matches in any directory. After the denies (the last matching rule wins), each existing file
    under cwd matching a secret_ok glob is allowed back by literal path."""
    lines = ["(version 1)", "(allow default)"]
    for g in secrets:
        g = os.path.expanduser(g) if g.startswith("~/") else g
        if '"' in g:  # the #"..." literal is raw: a quote cannot be carried, so it is refused
            raise ValueError("secrets glob holds a double quote, which a seatbelt regex cannot carry: " + g)
        rx = "^" + _glob_regex(g) + "$" if g.startswith("/") else "^(.*/)?" + _glob_regex(g) + "$"
        lines.append('(deny file-read* (regex #"' + rx + '"))')  # raw literal: single backslashes, as Apple's profiles write
    for f in _secret_ok_files(cwd, list(secret_ok)) if cwd and secret_ok else []:
        for p in dict.fromkeys([os.path.realpath(f), f]):  # seatbelt matches resolved paths (/tmp is /private/tmp); keep the given one too
            lines.append('(allow file-read* (literal "' + p.replace("\\", "\\\\").replace('"', '\\"') + '"))')
    return "\n".join(lines) + "\n"


def _bwrap_args(secrets, secret_ok, cwd, main, skip=()):
    """bwrap argv that hides every existing secrets file from a Linux lane: the whole tree stays bound, each secret is covered by
    /dev/null read-only, and the list ends with -- for the command. Absolute and ~/ entries are taken as given; bare globs are
    matched under cwd and under main (the main checkout), not descending into .git, node_modules, .venv or a skip entry (the
    [lanes] share folders). Files matching secret_ok are left visible."""
    absolute = lambda gs: [os.path.expanduser(g) for g in gs if g.startswith(("/", "~/"))]
    roots = list(dict.fromkeys([cwd, main]))
    pruned = {".git", "node_modules", ".venv"} | {str(x).strip("/") for x in skip}
    hide = {p for g in absolute(secrets) for p in glob.glob(g) if os.path.isfile(p)}
    ok = set(p for g in absolute(secret_ok) for p in glob.glob(g))
    for root in roots:
        for top, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in pruned and os.path.relpath(os.path.join(top, d), root).replace(os.sep, "/") not in pruned]
            for n in files:
                p = os.path.join(top, n)
                r = os.path.relpath(p, root).replace(os.sep, "/")
                hit = lambda gs: any(fnmatch.fnmatch(n, g) or fnmatch.fnmatch(r, g) for g in gs if not g.startswith(("/", "~/", guard.TRACKED)))
                if hit(secrets):
                    hide.add(p)
                if hit(secret_ok) or r in guard.tracked(root) and hit([g.removeprefix(guard.TRACKED) for g in secret_ok if g.startswith(guard.TRACKED)]):
                    ok.add(p)
    out = ["--dev-bind", "/", "/"]
    for p in sorted(hide - ok):
        out += ["--ro-bind", "/dev/null", p]
    return out + ["--"]


def os_guard(ctx, cwd):
    """The argv prefix that runs a call under the platform read guard (sandbox-exec on macOS, bwrap on Linux when on PATH), or
    None where there is no guard."""
    g = ctx.cfg["guards"]
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return ["sandbox-exec", "-p", _seatbelt_profile(g["secrets"], g.get("secret_ok", []), str(cwd))]
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return ["bwrap"] + _bwrap_args(g["secrets"], g.get("secret_ok", []), str(cwd), str(ctx.state.parent),
                                   ctx.cfg.get("lanes", {}).get("share", []))
    return None


def _git_dirs(cwd):
    """The absolute git dir and common dir of the checkout at cwd (in a worktree both sit outside it), or none outside git."""
    try:
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], cwd=cwd,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def _deny_reads(ctx, cwd):
    """Codex -c pieces for a profile on :workspace that denies reads of every [guards] secrets glob, wherever the file is
    named, turns the network on and lets the lane write its git dirs (a worktree's sit outside cwd, so commits need them).
    codex 0.167 takes only absolute or ~/ paths, and a glob only with "deny", so secret_ok (allow-lists) cannot be expressed
    here. :danger-full-access cannot be extended, so every lane_mode takes this profile."""
    roots = sorted({Path(r).as_posix() for r in (cwd, ctx.state.parent)} | {"~"})  # posix so Windows never mixes \ with /**/; ~ covers the home folder; codex 0.167 refuses a root of "/"
    pats = [p for g in ctx.cfg["guards"]["secrets"]
            for p in ([g] if g.startswith(("/", "~/")) else [f"{r}/**/{g}" for r in roots])]
    rules = [f"{json.dumps(d)} = \"write\"" for d in _git_dirs(cwd)] + [f"{json.dumps(g)} = \"deny\"" for g in pats]
    return ["-c", 'default_permissions="ade"', "-c", 'permissions.ade.extends=":workspace"',
            "-c", "permissions.ade.network.enabled=true", "-c", "permissions.ade.filesystem={" + ", ".join(rules) + "}"]


def _state_writes(ctx):
    """What a sandboxed lane may write beyond its cwd, so `ade` verbs can write state: ctx.state (the main checkout's .ade:
    q.sqlite, records rows from decide/card). Lanes never write git refs: records.append skips the branch backup
    inside a lane. It does not overlap a [guards] secrets path, so denyRead still wins."""
    return [str(ctx.state)]


def _secret_ok_files(cwd, globs):
    """Absolute paths of files under cwd matching a relative secret_ok glob (by name, or by path under cwd). Claude's sandbox
    lets only an exact-path allowRead override a glob denyRead. A file created later is not listed, so it stays denied."""
    rel = [g.removeprefix(guard.TRACKED) for g in globs if not g.startswith(("/", "~/"))]
    only = {g.removeprefix(guard.TRACKED) for g in globs if g.startswith(guard.TRACKED)}  # tracked: globs: tracked files only
    found = []
    # In a git checkout only tracked and untracked-but-not-ignored files are candidates, so an ignored venv, build output
    # or cache is never walked; outside one, or if git fails, every file but .git's is.
    try:
        ls = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=cwd,
                            capture_output=True, text=True, check=True).stdout
        paths = [os.path.join(cwd, n) for n in ls.split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        paths = []
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if d != ".git"]
            paths += [os.path.join(root, n) for n in files]
    for path in paths:
        n = os.path.basename(path)
        r = os.path.relpath(path, cwd).replace(os.sep, "/")
        if os.path.isfile(path) and any((fnmatch.fnmatch(n, g) or fnmatch.fnmatch(r, g)) and (g not in only or r in guard.tracked(cwd))
                                        for g in rel):
            found.append(path)
    return sorted(found)


def _lane_settings(ctx, card_id, cwd):
    """.ade/lanes/<card>.settings.json: the permission rules, and where Claude Code can sandbox (macOS, Linux with bwrap)
    a read sandbox denying [guards] secrets. Given by --settings, the rules hold in a checkout that is not a trusted folder."""
    cfg = ctx.cfg
    settings = {"permissions": {"allow": list(cfg["permissions"].get("allow", {})), "deny": install._denied(ctx)},
                "autoMemoryEnabled": False}  # a lane's context is the repo, the records and the card, never the host's memory
    if sys.platform == "darwin" or (sys.platform.startswith("linux") and shutil.which("bwrap")):
        form = lambda gs: [g if g.startswith(("/", "~/")) else "/**/" + g for g in gs]
        settings["sandbox"] = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                               "filesystem": {"denyRead": form(cfg["guards"]["secrets"]),
                                              "allowRead": form([g for g in cfg["guards"].get("secret_ok", []) if not g.startswith(guard.TRACKED)])
                                              + _secret_ok_files(cwd, cfg["guards"].get("secret_ok", [])),
                                              "allowWrite": _state_writes(ctx)}}
    p = ctx.state / "lanes" / f"{card_id}.settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with host.opentext(p, "w") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    return p


def _last_error_event(out):
    """The message of the last `error` / `turn.failed` JSON event on stdout (codex's real cause), else ''."""
    msg = ""
    for line in (out or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") in ("error", "turn.failed"):
            err = ev.get("error")
            m = ev.get("message") or (err.get("message") if isinstance(err, dict) else None)
            if isinstance(m, str) and m.strip() and not re.search(r"deny[-_ ]read.*glob|glob.*deny[-_ ]read", m, re.I):  # codex warns per call about unbounded ** deny globs
                msg = m.strip().splitlines()[-1]
    return msg


def _keep_stderr(ctx, card, err):
    """Whatever the engine wrote to stderr goes to the lane log after a marker, so `ade watch` shows its warnings."""
    if err and err.strip():
        with open(log_path(ctx, card["id"]), "a", encoding="utf-8") as f:
            f.write("\n--- stderr ---\n" + err.rstrip("\n") + "\n")


# How each engine takes a role's reasoning effort ([roles.*] effort = { claude = "high" }); unset passes no flag.
EFFORT_FLAG = {"claude": ["--effort", "{}"], "codex": ["-c", "model_reasoning_effort={}"], "opencode": ["--variant", "{}"]}


def effort_level(ctx, role, name, card=None):
    level = role_cfg(ctx, role, card).get("effort", {}).get(name)
    if not level and name == "codex":  # codex would otherwise take the user's ~/.codex effort
        level = ctx.cfg.get("providers", {}).get("codex", {}).get("default_effort")
    return level if name in EFFORT_FLAG else None


def effort_args(ctx, role, name, card=None):
    """The argv pieces that set `role`'s reasoning effort on engine `name` (the card's profile over it); none when unset."""
    level = effort_level(ctx, role, name, card)
    return [a.format(level) for a in EFFORT_FLAG[name]] if level else []


COMPACT_KEYS = (("auto_compact_token_limit", "model_auto_compact_token_limit"), ("context_window", "model_context_window"))


def compaction_args(ctx, name):
    """Codex's compaction limits when [providers.codex] sets them; none otherwise, so the lane inherits ~/.codex/config.toml."""
    pc = ctx.cfg.get("providers", {}).get("codex", {}) if name == "codex" else {}
    return [a for k, v in COMPACT_KEYS if pc.get(k) for a in ("-c", f"{v}={pc[k]}")]


def argv_for(ctx, role, name, prompt, cwd, card, extra=(), settings=None):
    """The engine argv of one call before host.which and the OS guard: [providers.<name>] cmd filled with prompt, model
    and the lane_mode's flags, then effort, compaction, the role's args, extra, and --settings <settings> when lane_settings is on."""
    spec, model = ctx.cfg["providers"][name], role_cfg(ctx, role, card)["models"][name]
    mode = ctx.cfg["permissions"]["lane_mode"]
    # A provider with a modes table gets the flag list for this lane_mode (an unlisted mode falls to "default"); without one, the mode string.
    if "modes" in spec:
        mode = spec["modes"].get(mode, spec["modes"].get("default", []))
    if "{deny_reads}" in mode:
        mode = [a for m in mode for a in (_deny_reads(ctx, cwd) if m == "{deny_reads}" else [m])]
    argv = host.substitute([a.replace("{prompt}", prompt).replace("{model}", model) for a in spec["cmd"]], mode=mode)
    if any("{prompt_file}" in a for a in argv):  # Pi reads the prompt as @file: a Windows .cmd launcher cuts argv at the first newline
        pf = ctx.state / "lanes" / f"{card['id']}.{role}.prompt.md"
        if prompt != "{prompt}":  # seats() lists with the placeholder and writes nothing
            pf.parent.mkdir(parents=True, exist_ok=True)
            host.write(pf, prompt)
        argv = [a.replace("{prompt_file}", str(pf)) for a in argv]
    argv += effort_args(ctx, role, name, card) + compaction_args(ctx, name) + role_cfg(ctx, role, card).get("args", {}).get(name, []) + list(extra)
    if name == "claude":  # added to Claude Code's own prompt, never replacing it; no subagents unless the seat opts in
        sp = system_prompt_file(ctx, role, card)
        argv += ["--append-system-prompt-file", str(sp)] if sp else []
        argv += [] if role_cfg(ctx, role, card).get("subagents") else ["--disallowedTools", "Agent"]
    if name == "codex":  # ADE's hooks as session flags, in a fixed order (install.codex_flags): one accept per machine
        from ade import install
        argv += install.codex_flags()
    return argv + (["--settings", str(settings)] if spec.get("lane_settings") else [])


TTL = "CLAUDE_CODE_PROMPT_CACHE_TTL"
# Host variables that change what a claude lane does or costs; --seats marks them wherever they are passed or dropped.
BEHAVIOUR = (TTL, "MAX_THINKING_TOKENS", "ENABLE_PROMPT_CACHING_1H", "FORCE_PROMPT_CACHING_5M", "CLAUDE_EFFORT",
             "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")
SYSTEM_PROMPT = ("--system-prompt", "--system-prompt-file", "--append-system-prompt", "--append-system-prompt-file")


def shown(ctx, env, n):
    """env's value for n, or only set/unset when n looks like a secret's name, so no report prints a key."""
    return ("set" if n in env else "unset") if _never(ctx.cfg, n) else env.get(n, "unset")


def system_prompt(argv):
    """sha8 of the system prompt argv passes (a *-file flag's file bytes, else the text itself), or 'none'."""
    for flag, val in zip(argv, argv[1:]):
        if flag in SYSTEM_PROMPT:
            p = Path(val).expanduser()
            return hashlib.sha256(p.read_bytes() if flag.endswith("-file") and p.is_file() else val.encode()).hexdigest()[:8]
    return "none"


def seat_line(ctx, role, name, card, argv, env, prompt=None):
    """The 'config:' line a call writes to its lane log under [report] seats; the prompt itself shows as {prompt}."""
    cmd = ["{prompt}" if a == prompt else a for a in argv]
    settings = cmd[cmd.index("--settings") + 1] if "--settings" in cmd else "none"
    ttl = shown(ctx, env, TTL) if TTL in env else "default"
    return (f"config: {role} {name} {role_cfg(ctx, role, card)['models'].get(name, '-')} "
            f"{effort_level(ctx, role, name, card) or '-'} {profile(ctx, card) or '-'} system_prompt={system_prompt(argv)} "
            f"ttl={ttl} settings={settings} argv={shlex.join(cmd)}\n")


def _marked(names):
    return ", ".join(k + (" (changes behaviour)" if k in BEHAVIOUR else "") for k in sorted(names)) or "none"


def _settings_files(ctx, spec, argv):
    """Lines for the Claude Code settings files a claude lane loads besides ADE's --settings: the model, env, permissions
    and hooks keys each sets, and a 'conflict:' line where one sets what ADE's argv overrides or scrub_env drops."""
    out = []  # read only: ADE never writes the user's file
    for label, p in [(lb, base / ".claude" / "settings.json") for lb, base in (("user", Path.home()), ("project", ctx.root))] \
            + [("lane settings.local.json", ctx.root / install.LOCAL)]:
        try:
            data = json.loads(host.read(p)) if p.is_file() else None
        except (OSError, ValueError):
            data = "unreadable"
        if not isinstance(data, dict):
            out.append(f"  settings {label} {p}: {data or 'absent'}")
            continue
        keys = [k for k in ("model", "env", "permissions", "hooks") if k in data]
        out.append(f"  settings {label} {p}: sets {', '.join(keys) or 'none of model, env, permissions, hooks'}")
        if "model" in data and "--model" in argv:
            out.append(f"  conflict: {p} sets model {data['model']}; ADE's --model {argv[argv.index('--model') + 1]} overrides it")
        if (data.get("permissions") or {}).get("defaultMode") and "--permission-mode" in argv:
            out.append(f"  conflict: {p} sets permissions.defaultMode; ADE's --permission-mode overrides it")
        out += [f"  conflict: {p} env sets {k}, which scrub_env drops from the host; Claude Code applies it anyway"
                for k in data.get("env") or {} if _scrubbed(spec, k, "claude")]
    return out


def seats(ctx):
    """`ade config --seats`: per role and engine, what a call is given, spending nothing: the 'config:' line with the
    exact argv, tools, voice and ruling count, env names passed and dropped, and Claude Code's settings files."""
    from ade.verbs import decide  # here, not at the top: decide imports the stages, which import this module
    rulings = sum(decide.state(d) == "ruling" for d in records.merged(ctx, "decision").values())
    card, out = {"id": "<card>", "title": "seats", "paths": []}, []
    for role, r in ctx.cfg["roles"].items():
        for name in [n for n in r["models"] if n in ctx.cfg["providers"]]:
            spec = ctx.cfg["providers"][name]
            settings = ctx.state / "lanes" / "<card>.settings.json" if spec.get("lane_settings") else None
            argv = argv_for(ctx, role, name, "{prompt}", ctx.root, card, (), settings)
            env = {k: v for k, v in os.environ.items() if not _scrubbed(spec, k, name)}
            dropped = [k for k in os.environ if _scrubbed(spec, k, name)]
            voice = role_cfg(ctx, role).get("voice", f"roles/{role}.voice.md")
            out += [f"[{role}] {name}{' (active)' if name == engine(ctx, role) else ''}",
                    "  " + seat_line(ctx, role, name, card, argv, env).rstrip(),
                    f"  subagents: {'off (--disallowedTools Agent)' if '--disallowedTools' in argv else 'on'}",
                    f"  tools: no allow-list in argv (roster {r.get('tools', '-')!r} binds the .claude/agents subagent; a lane is"
                    " bounded by its permission mode and settings)",
                    f"  voice: {voice}{'' if (ctx.root / voice).is_file() else ' (absent)'}; profiles: {', '.join(profiles(ctx)) or 'none'};"
                    f" rulings: {rulings} live (a call carries standing rulings and those matching its card's files, up to {ctx.cfg['budgets']['rulings']})",
                    f"  env passed: {_marked(set(env) | set(spec.get('secrets', {})))}", f"  env scrubbed: {_marked(dropped)}"]
            out += [f"  conflict: host env sets {k}={shown(ctx, os.environ, k)}; scrub_env drops it" for k in sorted(dropped) if k in BEHAVIOUR]
            if "ANTHROPIC_MODEL" in env and "--model" in argv:
                out.append(f"  conflict: host env sets ANTHROPIC_MODEL={shown(ctx, env, 'ANTHROPIC_MODEL')}; ADE's --model overrides it")
            if name == "claude":
                out += _settings_files(ctx, spec, argv)
            if settings:
                out.append(f"  settings ADE --settings {settings}: sets permissions (and the read sandbox where available), written per call")
                out.append("  lane memory: off (autoMemoryEnabled=false in --settings)")
    return out


def call(ctx, role, prompt, cwd, card, *a, **kw):
    """_call, with the card's answered questions not yet delivered appended to the prompt ('a question on this card was
    answered: QUESTION -> ANSWER') and marked delivered (the 'reworked' field) once the call returns."""
    from ade.verbs import decide
    qs = decide.undelivered(ctx, card["id"])
    prompt += "".join(f"\n\na question on this card was answered: {d['text']} -> " + (d["answer"] if "answer" in d else f"{d['default']} (the default that applied)") for d in qs)
    out = _call(ctx, role, prompt, cwd, card, *a, **kw)
    for d in qs:
        records.append(ctx, "decision", {"id": d["id"], "reworked": records.today().isoformat()})
    return out


UNHOOKED = {}  # card id -> the engine whose last builder call left no start-hook mark (read by the build stage)
NAMED = {}  # card id -> True when that engine is already named in a role's models (init has written its hooks)
BACKGROUNDED = {}  # card id -> jobs whose background_wait ended the last builder call


def unhooked(card_id):
    """The build stage's notes about backgrounded jobs and missing hooks from its last builder call."""
    bg = BACKGROUNDED.get(card_id)
    note = f"; opencode moved {bg} to the background; ADE ended the call" if bg else ""
    name = UNHOOKED.get(card_id)
    if not name:
        return note
    if name == "codex":
        fix = "accept codex's one-time hook trust"
    elif NAMED.get(card_id):  # the engine is already in the roles: init wrote its hook file, so check that
        hook_file = {"claude": install.LOCAL, "opencode": ".opencode/plugin/ade.ts", "pi": ".pi/extensions/ade.ts"}.get(name, "its hook file")
        fix = f"check {name}'s hook settings in {hook_file}, then rerun `ade init`"
    else:
        fix = f"name {name} in a role's models in ade.toml, then rerun `ade init`"
    return note + f"; ADE's hooks did not run in that lane ({name} left no start mark): {fix}"


def _call(ctx, role, prompt, cwd, card, extra=(), session=None, name=None):
    """Run one provider call in the lane and return its answer text. Every call, the fake included, writes one
    `usage` row from the provider's own report (telemetry.parse), so spend is counted where it happens. `extra`
    is argv appended after the template's (fork_args pieces, with {session} filled by the caller)."""
    name, started, prof = engine(ctx, role, name, card), time.monotonic(), profile(ctx, card)
    if role == "builder":
        BACKGROUNDED.pop(card["id"], None)
    if name == "fake":
        text = _fake(role, cwd, card, prompt) + (f"fake: session {session}\n" if session else "")
        with host.opentext(log_path(ctx, card["id"], role, name), "a") as f:
            f.write(text)
        # The fake truly spends nothing, so its zeros are a reading, not a missing report.
        telemetry.record(ctx, card["id"], STAGE.get(role, role), role, "fake", "fake", time.monotonic() - started,
                         {k: 0 for k in telemetry.KINDS}, profile=prof)
        return text
    sp = system_prompt_file(ctx, role, card)
    if sp and name != "claude":  # codex and opencode have no append flag: the text leads the prompt
        prompt = f"# Project system prompt\n\n{host.read(sp).strip()}\n\n{prompt}"
    spec = ctx.cfg["providers"].get(name)
    model = role_cfg(ctx, role, card)["models"].get(name)
    if not spec or not model:
        refuse("provider.setup", f"no [providers.{name}] cmd or {role} model for {name} in ade.toml",
               f"configured engines: {', '.join(ctx.cfg['providers'])}; add both to ade.toml, or set ADE_PROVIDER=fake to rehearse")
    lane = _lane_settings(ctx, card["id"], cwd) if spec.get("lane_settings") else None
    sandbox = "claude" if lane and '"sandbox"' in host.read(lane) else None
    argv = argv_for(ctx, role, name, prompt, cwd, card, extra, lane)
    if not shutil.which(argv[0]):
        refuse("provider.setup", f"{argv[0]} is not on PATH", f"install {argv[0]}, or set ADE_PROVIDER=fake to rehearse")
    argv, stdin = host.which(argv), None
    if spec.get("sandbox") == "os":
        guard = os_guard(ctx, cwd)  # no guard here: runs unwrapped, and `ade init` says so
        argv, sandbox = (guard or []) + argv, ("os" if guard else sandbox)
    # npm installs claude and codex on Windows as .cmd shims, and cmd.exe ends every argument at the first newline
    # (dropping the flags after it too), so there a whole-token prompt goes on stdin, which both engines read when
    # no prompt is given. Platform: a cmd.exe-safe argv from subprocess, or native engine binaries, would delete this.
    # A prompt over BIG_PROMPT bytes goes on stdin on every platform: argv that large fails Popen (E2BIG, Errno 7).
    nt_cmd = os.name == "nt" and Path(argv[0]).suffix.lower() in (".cmd", ".bat")
    if (nt_cmd or (name in ("claude", "codex") and len(prompt.encode("utf-8")) > BIG_PROMPT)) and prompt in argv:
        argv, stdin = [a for a in argv if a != prompt], prompt
    # stdout streams into the lane log as it is written, so `ade watch` (and an Orca terminal running it) can follow
    # the engine live; the call's report is then read back from the bytes this call added.
    mark = ctx.state / "lanes" / f"{card['id']}.{role}.hooked"  # the lane's start hook touches it (ade hook start)
    mark.unlink(missing_ok=True)
    env = {**_env(spec, name), "ADE_ROLE": role, "ADE_MARK": str(mark), "PWD": str(cwd), **ade_bin(), **({"ADE_SANDBOX": sandbox} if sandbox else {})}  # ADE_SANDBOX: a tool tells a sandboxed lane from an open one; ADE_ROLE: the lane's SessionStart hook hands over this role; ADE_BIN: opencode and Pi adapters run this kit
    if name == "opencode":  # opencode's title call uses its own small model unless pinned; the user's own setting wins
        try:
            mine = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"small_model": model, **mine})
        except (ValueError, TypeError):
            pass  # not a JSON object: leave the user's value alone
    with open(log_path(ctx, card["id"], role, name), "ab") as log:
        if ctx.cfg.get("report", {}).get("seats"):  # before the engine starts, so `ade watch` shows what this call was given
            log.write(seat_line(ctx, role, name, card, argv, env, prompt).encode("utf-8"))
            log.flush()  # the line is on disk before the engine (sharing this file) writes its first byte
        start = log.tell()
        timeout = spec.get("timeout", 1800)
        # The engine gets its own process group so a timeout can kill an npm wrapper's native child too.
        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                 else {"start_new_session": True})
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.PIPE, stdin=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", **group,
                                    env=env)
        except OSError as e:  # e.g. Argument list too long: a stop for this card, never a traceback
            telemetry.record(ctx, card["id"], STAGE.get(role, role), role, name, model, time.monotonic() - started,
                             {"failed": "start"}, profile=prof)
            refuse("provider.failed", f"{name} could not start: {e.strerror or e}",
                   f"shrink the prompt, or `ade loop {card['id']}`")
        job = host.job_new(proc)  # Windows: the engine's tree lives in a Job Object, ended below (None elsewhere)
        # Besides the timeout, ade watches the lane log's size: no new byte for `stall` seconds (0 = off) is a silent engine.
        stall, begun, seen, grew, silent = spec.get("stall", 0), time.monotonic(), start, time.monotonic(), False
        scanned, bg = start, None  # opencode 2.x: a background_wait event means the answer is in and a moved job keeps the process alive
        try:
            while True:
                left = timeout - (time.monotonic() - begun)
                if left <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    out, err = proc.communicate(stdin, timeout=min(left, 1) if stall or name == "opencode" else left)
                    break
                except subprocess.TimeoutExpired:
                    stdin = None  # communicate keeps what it was given
                    size = os.fstat(log.fileno()).st_size
                    if size != seen:
                        seen, grew = size, time.monotonic()
                        if name == "opencode":
                            bg, scanned = _background_wait(log_path(ctx, card["id"]), scanned)
                            if bg is not None:
                                break
                    elif stall and time.monotonic() - grew >= stall:
                        silent = True
                        raise
            if bg is not None:  # end the group now; what the model answered and edited stays
                if os.name == "nt":
                    if job:
                        host.job_end(job)
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.kill()
                out, err = proc.communicate()
                proc.returncode = 0
            r = subprocess.CompletedProcess(argv, proc.returncode, out, err)
            # A normal exit ends the group too: a leftover child would hold the lane's git lock.
            if os.name == "nt":
                if job:
                    host.job_end(job)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                if job:
                    host.job_end(job)
                else:  # job setup failed: the old tree walk, on the timeout path only
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.kill()
            err = proc.communicate()[1]  # reap
            r = None
    if r is None:  # a hung engine is a stop like any failed call, never a traceback
        _keep_stderr(ctx, card, err)
        telemetry.record(ctx, card["id"], STAGE.get(role, role), role, name, model, time.monotonic() - started,
                         {"failed": "stall" if silent else "timeout"}, profile=prof)
        if silent:
            refuse("provider.failed", f"{name} was silent for {stall}s",
                   f"raise [providers.{name}] stall, or `ade loop {card['id']}`")
        refuse("provider.failed", f"{name} outlived its {timeout}s timeout",
               f"raise [providers.{name}] timeout, or `ade loop {card['id']}`")
    try:
        with open(log_path(ctx, card["id"]), "rb") as log:
            log.seek(start)
            r.stdout = log.read().decode("utf-8", "replace")
    except FileNotFoundError:  # another run's land moved the log away mid-call: a stop, never a traceback
        telemetry.record(ctx, card["id"], STAGE.get(role, role), role, name, model, time.monotonic() - started,
                         {"failed": "log missing"}, profile=prof)
        refuse("provider.failed", f"{name} finished but the lane log .ade/lanes/{card['id']}.log is gone (moved by another run's land?)",
               f"`ade loop {card['id']}`")
    if name == "pi":
        _trim_pi_log(log_path(ctx, card["id"]), start)
    _keep_stderr(ctx, card, r.stderr)  # after the read-back: telemetry and the capacity check read stdout alone
    text, fields = telemetry.parse(r.stdout)
    if bg is not None and role == "builder":
        BACKGROUNDED[card["id"]] = bg
    telemetry.note(ctx, "claude", "lane stream", fields.pop("limits", None))
    # A lane given CLAUDE_CODE_PROMPT_CACHE_TTL=1h (pass_env) writes 1-hour cache on purpose, so that is no sign.
    if name == "claude" and "ANTHROPIC_API_KEY" in spec.get("secrets", {}) and fields.get("cache_write_1h", 0) > 0 \
            and env.get("CLAUDE_CODE_PROMPT_CACHE_TTL") != "1h":
        fields["billing_suspect"] = True  # a key is set, yet 1-hour cache writes are what a plan login makes
        print("claude call wrote 1-hour cache with an API key set: the lane may have inherited a host's plan login "
              "(for example CLAUDE_CODE_REMOTE), so it may not be billing the key "
              "(see https://atlas-ade.dev/docs/run-lanes-on-api-credits/)", file=sys.stderr)
    # A failed call can still have spent; its row is written before the refusal, marked as failed.
    telemetry.record(ctx, card["id"], STAGE.get(role, role), role, name, model, time.monotonic() - started,
                     {"served_model": None, **fields, "effort": effort_level(ctx, role, name, card), **({"failed": r.returncode} if r.returncode else {})}, profile=prof)
    # An engine at capacity can exit 0, its few lines then read as a finished answer: a stop of its own, whatever the exit code.
    # Only a short answer can be that report: a long one may quote the error while discussing it, so it is not scanned.
    # With no parsed answer (text is the raw stdout, as for codex's error events) the last raw lines are all there is.
    # `is` is deliberate: telemetry.parse returns its stdout argument itself when it found no report (see its docstring).
    answer = [l for l in text.lower().splitlines() if l.strip()]
    answer = answer[-3:] if text is r.stdout else answer if len(answer) <= 3 else []
    if name == "claude" and any(CREDIT in line for line in answer):  # the answer alone: stderr noise must not hide or fake it
        refuse("provider.credit", f"claude: {CREDIT} (the key [providers.claude] secrets names is out of credit)",
               f"top up that key, then ade loop {card['id']}")
    last = answer + (r.stderr or "").strip().lower().splitlines()[-3:]
    for phrase in CAPACITY:
        if any(phrase in line for line in last):
            reset = next((l.strip() for l in answer if phrase in l and "reset" in l), "")  # the answer's own reset time, when it names one
            refuse("provider.capacity", f"{name} is at capacity ({phrase!r})" + (f": {reset}" if reset else ""), f"wait, then `ade loop {card['id']}`")
    if r.returncode:
        tail = [_last_error_event(r.stdout)] if _last_error_event(r.stdout) else (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no output"]
        refuse("provider.failed", f"{name} exited {r.returncode}: {tail[0]}", f"read the lane at {cwd}, then `ade loop {card['id']}`")
    if role == "builder":
        UNHOOKED[card["id"]] = None if mark.exists() else name
        written = config.merge(tomllib.loads(host.read(config.TEMPLATE)), tomllib.loads(host.read(ctx.root / "ade.toml")))
        NAMED[card["id"]] = name in install.engines(config.Ctx(ctx.root, written))  # ade.toml as written, not this run's --model
    return text


def _is_update(line):
    if b'"message_update"' not in line:
        return False
    try:
        return json.loads(line).get("type") == "message_update"
    except (ValueError, AttributeError):
        return False


def _trim_pi_log(path, start):
    """Pi's --mode json repeats the whole partial message in every message_update, so a long lane's log grows quadratically:
    once the call is read, rewrite its bytes (from start) without those lines; every other line stays, in order."""
    try:
        with open(path, "r+b") as f:
            f.seek(start)
            lines = f.read().splitlines(keepends=True)
            keep = [l for l in lines if not _is_update(l)]
            if len(keep) < len(lines):
                f.seek(start)
                f.write(b"".join(keep))
                f.truncate()
    except OSError:
        pass  # the log moved or is unwritable: the full log stays, nothing is lost


def _background_wait(path, offset):
    """(job, new offset): the job a `background_wait` JSON line (from offset on) names, else None."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return None, offset
    end = data.rfind(b"\n") + 1  # whole lines only
    for line in data[:end].decode("utf-8", "replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "background_wait":
            names = [j.get("title") or j.get("id") for j in obj.get("jobs", []) if isinstance(j, dict)]
            return ", ".join(n for n in names if isinstance(n, str) and n) or "a command", offset + end
    return None, offset + end


def _fake(role, cwd, card, prompt=""):
    # What it was told leads, so the answer still ends as the role's own fake does (a builder's DONE stays last).
    saw = "".join(f"fake saw: {l}\n" for l in prompt.splitlines() if l.startswith("a question on this card was answered"))
    return saw + _fake_say(role, cwd, card, prompt)


def _fake_say(role, cwd, card, prompt):
    if role == "reviewer":
        return "APPROVE\nfake reviewer: no findings\n"
    if role == "pilot" and card["id"] == "draft":
        # The first file `ade card draft` lists (its 'path: what' lines), so the drafted card is valid.
        first = prompt.partition("The repo's files")[2].partition("\n")[2].splitlines()[0].partition(":")[0]
        return f"TITLE: {card['title']}\nPATH: {first}\nDONE: the fake pilot's draft lands\n"
    if role != "builder":
        return f"fake {role}: looked, changed nothing\n"
    # The fake builder makes one harmless, valid edit to the card's first literal path.
    target = next((p for p in card["paths"] if not any(c in p for c in "*?[")), None)
    if target is None:
        return "fake builder: no literal path to edit\nDONE\n"
    p = Path(cwd) / target
    p.parent.mkdir(parents=True, exist_ok=True)
    mark = "//" if p.suffix in (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs") else "#"
    # A new file gets the five-line header, as the brief asks of every builder, so a rehearsal can land it.
    head = "" if p.exists() else "".join(f"{mark} {k}: {v}\n" for k, v in (
        ("id", Path(target).with_suffix("").as_posix().replace("/", ".")), ("what", card["title"]),
        ("cmd", "-"), ("reads", "-"), ("writes", "-")))
    with host.opentext(p, "a") as f:
        f.write(f"{head}{mark} {card['title']}\n")
    return f"fake builder edited {target}\nDONE\n"
