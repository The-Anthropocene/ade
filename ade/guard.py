# id: ade.guard
# what: the bash guard: refuse the named classes only (secret reads, destructive deletes, history rewrite, protected config writes)
# cmd: -
# reads: ade.toml [guards], [protected], git ls-files (tracked: secret_ok)
# writes: -
import os
import posixpath
import functools
import re
import shlex
import subprocess
import tempfile

from ade.config import matches
from ade.refuse import refuse

OPS = {"&&", "||", ";", "|", "|&", "&", "\n", "(", ")"}
WRAPPERS = {"sudo", "env", "command", "nohup", "time", "xargs", "nice", "timeout", "exec", "stdbuf", "busybox", "watch"}
# Options of a wrapper that take their own value as the next word (`sudo -u x`, `nice -n 5`); timeout also takes a duration.
WRAP_VALUED = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}, "env": {"-u", "-C", "-S"},
               "nice": {"-n"}, "timeout": {"-s", "-k"}, "stdbuf": {"-i", "-o", "-e"}, "watch": {"-n", "-d"},
               "exec": {"-a"}, "xargs": {"-n", "-I", "-L", "-P", "-s", "-d", "-E", "-a"}}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "{", "!"}
REDIRECT = {">", ">>", ">|", "&>", "&>>"}
SHELLS = {"bash": "-c", "sh": "-c", "zsh": "-c", "dash": "-c", "pwsh": "-c", "powershell": "-c", "cmd": "/c"}
# PowerShell and cmd forms, judged like rm / mv / redirection (command names compare lower-case, .exe dropped).
DELETERS = {"remove-item", "ri", "rm", "del", "erase", "rd", "rmdir", "unlink", "shred"}
MOVERS = {"move-item", "mi", "move", "mv"}
WRITERS = {"set-content", "sc", "add-content", "ac", "out-file", "new-item", "ni", "copy-item", "cp", "copy", "cpi",
           "tee-object", "tee", "clear-content", "clc", "rename-item", "rni", "ren", "rsync"}
COPIERS = {"cp", "install", "rsync"}
RECURSIVE = {"-r", "-rec", "-recurse", "/s"}
# Deleting these loses what git and the records hold, whatever else the card owns.
KEEP = (".git", ".ade")
GIT_VALUED = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--exec-path"}


def segments(command):
    """Simple commands of a shell line, operators split off; a redirect target stays as `>` then the path."""
    bodies = []

    def _heredoc(m):
        line = m.string[m.string.rfind("\n", 0, m.start()) + 1:m.start()]
        fed = segments(line)  # the command the heredoc feeds is the last simple command before it
        if fed and posixpath.basename(fed[-1][0]) in ("bash", "sh", "zsh", "dash"):
            bodies.append(m.group(4))  # fed to a shell, the body is commands: judged below as lines
        return "<<" + m.group(2) + m.group(3)

    command = re.sub(r"<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", _heredoc, command, flags=re.S)  # any other heredoc body is data
    if os.name != "nt":  # bash folds a backslash-newline away; on Windows a backslash is a path separator, so it is left alone
        command = re.sub(r"(?<!\\)((?:\\\\)*)\\\n", r"\1", command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lex.whitespace_split = True
    lex.whitespace = " \t\r"  # a newline ends a command; only a quoted one stays inside its word
    lex.commenters = ""
    if os.name == "nt":
        lex.escape = ""  # backslashes are path separators there, not escapes
    try:
        toks = list(lex)
    except ValueError:
        return [command.split()]  # unbalanced quotes: judge the words as they stand
    out, cur = [], []
    for t in (p for t in toks for p in (re.findall(r"&>>?|[<>]+[&|]?|&&|\|[|&]?|[;&()\n]", t) if t and t[0] in ";&|<>()\n" else [t])):  # a glued run (`);`, `&&(`, `|&`) is several tokens
        if t in OPS:
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    out.append(cur)
    words = [w for b in bodies for w in segments(b.replace("\n", " ; "))]
    for seg in out:
        while seg:
            if seg[0] in KEYWORDS or "=" in seg[0] and not seg[0].startswith("-"):
                seg = seg[1:]  # `then cat`, `{ cat`, `env X=1 cat`: judge the command underneath
            elif seg[0] == "for":
                seg = []  # a `for x in a b` head runs nothing
            elif seg[0] in WRAPPERS:
                w, seg = seg[0], seg[1:]  # `sudo -u x rm`, `timeout 5 cat`: skip the wrapper, its options and their values
                while seg and seg[0].startswith("-"):
                    seg = seg[2:] if seg[0] in WRAP_VALUED.get(w, ()) else seg[1:]
                if w == "timeout" and seg:
                    seg = seg[1:]
            else:
                break
        if seg:
            words.append(seg)
    return words


def _name(tok):
    return posixpath.basename(tok.replace("\\", "/"))


def _cmd(tok):
    return _name(tok).lower().removesuffix(".exe")


def _join(cwd, tok):
    # Join onto cwd unless the token is absolute: posix, drive (C:/x) or UNC (//host/share), on every platform.
    t, c = tok.replace("\\", "/"), cwd.replace("\\", "/")
    if len(t) > 1 and t[1] == ":" and t[0].isalpha() or t.startswith("//"):
        return posixpath.normpath(t)
    return posixpath.normpath(posixpath.join(c, t))


def _forms(tok, root, cwd):
    # The basename, the repo-relative path and the home-relative path: [guards] globs may name any of them.
    t = tok.replace("\\", "/")
    out = [_name(tok), _rel(tok, root, cwd)]
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/") + "/"
    if t.startswith("~/"):
        out.append(posixpath.normpath(t[2:]))
        t = home + t[2:]
    elif t.startswith(home):
        out.append(posixpath.normpath(t[len(home):]))
    # The absolute path and each trailing run of its components, so a glob matches the file wherever it is named.
    full = _join(cwd, t)
    parts = full.strip("/").split("/")
    out += [full] + ["/".join(parts[i:]) for i in range(len(parts))]
    return [f for f in out if f]


def _any(forms, globs):
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    return any(matches(f, [home + g[1:] if g.startswith("~/") else g for g in globs]) for f in forms)


PATTERNS = {"grep": ("-e", "--regexp"), "egrep": ("-e", "--regexp"), "rg": ("-e", "--regexp"), "sed": ("-e", "--expression"), "awk": ()}


def _script(cmd, text):
    # File names a sed script (r/R/w/W commands) or an awk program (any quoted string: getline < "F", print > "F") names.
    if cmd.lower() == "sed":
        address = r"(?:\d+|\$|/(?:\\.|[^/\\])*/)"
        return re.findall(r"(?:^|[;\n{}])[ \t]*(?:" + address + r"(?:[ \t]*,[ \t]*" + address + r")?)?[ \t]*!?[ \t]*[rRwW][ \t]*([^\s;}]+)", text)
    if cmd.lower() == "awk":
        return re.findall(r'"([^"]+)"', text)
    return []


def _files(cmd, args):
    # A grep/sed/awk pattern or script is not a path: the first operand is one unless -e/-f supplied it; -f's value is a file.
    pats, given, out, i, end = PATTERNS.get(cmd.lower()), False, [], 0, False
    while i < len(args):
        a = args[i]
        if a == "--" and not end:  # ends flags: every later argument is an operand
            end = True
        elif pats is not None and not end and a in pats + ("-f", "--file"):
            given, i = True, i + 1
            if a in ("-f", "--file") and i < len(args):
                out.append(args[i])
            elif i < len(args):
                out += _script(cmd, args[i])
        elif not end and a.startswith("--") and "=" in a:  # --long=VALUE: judge the value, as if it followed the option
            option, value = a.split("=", 1)
            if pats is not None and option in pats + ("--file",):
                given = True
            if option not in (pats or ()):
                out.append(value)
            else:
                out += _script(cmd, value)
        elif end or not a.startswith("-"):
            if pats is not None and not given:
                given = True
                out += _script(cmd, a)
            else:
                out.append(a)
        i += 1
    return out if pats is not None else [a.split("=", 1)[1] if a.startswith("--") and "=" in a else a
                                         for a in args if not a.startswith("-") or a.startswith("--") and "=" in a]


TRACKED = "tracked:"  # a secret_ok glob written 'tracked:GLOB' allows a match back only while git tracks the file


@functools.lru_cache(maxsize=None)
def tracked(root):
    """Repo-relative posix paths git tracks under root, listed once per call; empty when git is unavailable (fail closed)."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return frozenset()
    return frozenset(n for n in out.decode("utf-8", "replace").split("\0") if n)


def secret_ok(globs, forms, root, rel):
    """Whether a path (its match forms and repo-relative rel) is allowed back by secret_ok: a plain glob, or a tracked: glob
    while git tracks it."""
    plain = [g for g in globs if not g.startswith(TRACKED)]
    only = [g[len(TRACKED):] for g in globs if g.startswith(TRACKED)]
    return _any(forms, plain) or bool(only and rel in tracked(root) and _any(forms, only))


def secret(cfg, tok, root, cwd):
    g = cfg["guards"]
    forms = _forms(tok, root, cwd)
    return _any(forms, g["secrets"]) and not secret_ok(g["secret_ok"], forms, root, _rel(tok, root, cwd))


def _rel(tok, root, cwd):
    # Repo-relative posix path of a token, so [protected] globs apply to `./ade.toml` and absolute paths alike.
    p, r = _join(cwd, tok), root.replace("\\", "/").rstrip("/") + "/"
    if len(p) > 1 and p[1] == ":":  # drive letters compare case-insensitively
        p, r = p[0].lower() + p[1:], r[0].lower() + r[1:]
    return p[len(r):] if p.startswith(r) else None


def _kept(a, root, cwd):
    rel = _rel(a, root, cwd)
    return rel is not None and any(rel == k or rel.startswith(k + "/") for k in KEEP)


def _temp(a, cwd):
    # Strictly inside the system temp folder (gettempdir, $TMPDIR/$TMP/$TEMP, /tmp /private/tmp /var/tmp on POSIX), with those three names expanded first.
    for n in ("TMPDIR", "TMP", "TEMP"):
        if os.environ.get(n):
            a = a.replace("${%s}" % n, os.environ[n]).replace("$" + n, os.environ[n])
    if "$" in a or "`" in a or a == "*" or a.endswith("/*"):
        return a, False
    bases = {tempfile.gettempdir(), *filter(None, map(os.environ.get, ("TMPDIR", "TMP", "TEMP")))}
    if os.name == "posix":
        bases |= {"/tmp", "/private/tmp", "/var/tmp"}  # macOS TMPDIR is /var/folders/..., yet /tmp is still scratch
    full = _join(cwd, a)
    low = lambda s: s[:2].lower() + s[2:] if s[1:2] == ":" else s  # drive letters compare case-insensitively
    return a, any(low(full).startswith(low(posixpath.normpath(b.replace("\\", "/"))) + "/") for b in bases)


def _wide(a, root, cwd):
    a, inside = _temp(a, cwd)
    if inside and _rel(a, root, cwd) is None and not (posixpath.normpath(root.replace("\\", "/")) + "/").startswith(_join(cwd, a) + "/"):
        return False  # the repo itself, or a folder holding it, is never scratch
    if a in ("/", ".", "..", "*") or a.startswith("~") or "$" in a or "`" in a:
        return True  # root, home or anything under it (`~/x`), the repo itself, or a target the guard cannot resolve: fail closed
    return _rel(a, root, cwd) in (None, "", ".")


def cure(cfg, rel):
    # The cure for a refused protected path: the first matching [protected] cures entry, else the default.
    for glob, text in cfg["protected"].get("cures", {}).items():
        if matches(rel, [glob]):
            return text
    return "change it by hand (ade.toml), then rerun `ade init` to regenerate the rest"


def _inert(command):
    """The command with single-quoted text and quoted-heredoc bodies emptied: the shell never runs a substitution there."""
    command = re.sub(r"(<<-?[ \t]*(['\"]?)\\?(\w+)\2[^\n]*\n)(.*?)(\n[ \t]*\3[ \t]*(?=\n|$))", lambda m: m.group(1) + m.group(5) if m.group(2) == "'" else m.group(0), command, flags=re.S)
    return re.sub(r"""(\\.|"(?:\\.|[^"\\])*")|'[^']*'""", lambda m: m.group(1) or "''", command, flags=re.S)


def check(cfg, command, root, cwd, branch):
    """Refuse the first named class the command falls in; return quietly otherwise."""
    for tick, sub in re.findall(r"`([^`]*)`|\$\(([^()]*)\)", _inert(command)):
        check(cfg, tick or sub, root, cwd, branch)  # a backtick or $(...) body runs as a line of its own
    for seg in segments(command):
        cmd, args = _cmd(seg[0]), seg[1:]
        if cmd == "cd" and args and not args[0].startswith("-"):
            cwd = _join(cwd, args[0])  # a `cd X &&` in the chain moves where later relative paths land
        if cmd == "eval" and args:
            check(cfg, " ".join(args), root, cwd, branch)
        flag = SHELLS.get(cmd)
        if flag:
            # `-c`, or a short-flag cluster ending in c (`-lc`, `-ec`): the next word is a line to judge in turn.
            hits = [i for i, a in enumerate(args[:-1]) if a.lower() == flag.lower()
                    or flag == "-c" and a.startswith("-") and not a.startswith("--") and a.lower().endswith("c")
                    or flag == "-c" and cmd in ("pwsh", "powershell") and a.lower() == "-command"]
            if hits:
                check(cfg, args[hits[0] + 1], root, cwd, branch)
        if cmd == "ade" and args[:2] == ["q", "run"]:
            # `ade q run [--card ID] -- CMD` runs CMD for the agent: judge CMD, or the store becomes a way round the guard.
            rest = args[2:]
            if rest[:1] == ["--card"]:
                rest = rest[2:]
            rest = rest[1:] if rest[:1] == ["--"] else rest
            if rest:
                check(cfg, rest[0] if len(rest) == 1 else shlex.join(rest), root, cwd, branch)
        flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
        if cmd in map(str.lower, cfg["guards"]["readers"]) and any(secret(cfg, a, root, cwd) for a in _files(cmd, args)):
            refuse("guard.secret", f"`{shlex.join(seg)}` would read a secret file",
                   "pass the file by path to the tool that needs it (`--env-file .env`); never print it")
        if cmd in ("cp", "rsync", "scp"):
            sources = _files(cmd, args)[:-1]
            if cmd == "cp":
                # With -t/--target-directory the destination is an option, so every operand is a source.
                for i, a in enumerate(args):
                    if a == "--":
                        break
                    if a in ("-t", "--target-directory"):
                        sources = _files(cmd, args[:i] + args[i + 2:])
                        break
                    if a.startswith(("-t", "--target-directory=")):
                        sources = _files(cmd, args[:i] + args[i + 1:])
                        break
            if any(secret(cfg, a, root, cwd) for a in sources):
                refuse("guard.secret", f"`{shlex.join(seg)}` would copy a secret file out",
                       "pass the file by path to the tool that needs it (`--env-file .env`); never copy it")
        paths = [a for a in args if not a.startswith("-") and not re.fullmatch(r"/[a-z?]", a, re.I)]
        keep = [a for a in paths if _kept(a, root, cwd)]
        if (cmd in DELETERS or cmd in MOVERS) and keep:
            refuse("guard.destructive", f"`{shlex.join(seg)}` removes {keep[0]}, which holds git history or the records",
                   "leave .git and .ade to git and the ade verbs")
        if cmd in DELETERS and (cmd == "rm" and "r" in flags.lower() or "--recursive" in args
                                or any(a.lower() in RECURSIVE for a in args)):
            wide = [a for a in paths if _wide(a, root, cwd)]
            if wide:
                refuse("guard.destructive", f"`{shlex.join(seg)}` deletes {wide[0]}, beyond anything one card owns",
                       "delete the specific paths inside the repository instead")
        if cmd == "git":
            while args and args[0].startswith("-"):  # git's global options come before the subcommand
                args = args[2:] if args[0] in GIT_VALUED else args[1:]
        if cmd == "git" and args:
            sub, rest = args[0], args[1:]
            force = sub == "push" and (any(a in ("-f", "--force", "--force-with-lease", "--mirror") or
                                           a.startswith("--force") for a in rest)
                                       or any(a.startswith("+") for a in rest))
            trunk = cfg["git"]["main"]
            names = [a for a in rest if not a.startswith("-")]
            short = "".join(a.lstrip("-") for a in rest if a.startswith("-") and not a.startswith("--"))
            deleting = "--delete" in rest or "d" in short.lower()
            gone = (sub == "push" and ("--delete" in rest or "d" in short) and trunk in names
                    or sub == "push" and any(a.lstrip("+") in (":" + trunk, ":refs/heads/" + trunk) for a in names)
                    or sub == "branch" and deleting and trunk in names
                    or sub == "update-ref" and "-d" in rest and "refs/heads/" + trunk in names
                    or sub == "tag" and deleting)
            on_main = branch == trunk and (
                sub == "commit" and "--amend" in rest or sub == "reset" and "--hard" in rest)
            if sub == "clean" and any(a.startswith("-") and not a.startswith("--") and "x" in a.lower() for a in rest):
                refuse("guard.destructive", f"`{shlex.join(seg)}` also deletes ignored files, .ade/ records among them",
                       "drop -x/-X, or delete the specific ignored paths")
            if force or gone or sub in ("filter-branch", "filter-repo") or on_main:
                refuse("guard.history", f"`{shlex.join(seg)}` rewrites history",
                       "add a new commit that undoes or fixes the change instead")
            if sub in ("switch", "checkout") and trunk in names:
                branch = trunk  # later segments of a chain may run on trunk; path checkouts never clear it
        targets = _targets(cfg, cmd, args)
        hit = [t for t in targets if (_rel(t, root, cwd) or "") and matches(_rel(t, root, cwd), cfg["protected"]["paths"])]
        if hit:
            refuse("guard.protected", f"`{shlex.join(seg)}` would change protected config {hit[0]}",
                   cure(cfg, _rel(hit[0], root, cwd)))


def _targets(cfg, cmd, args):
    # Tokens a segment writes: redirect targets, [guards] writers' args, `sed -i` / `perl -i` files.
    targets = [args[i + 1] for i, a in enumerate(args[:-1]) if a in REDIRECT]
    inplace = cmd in ("sed", "perl") and any(re.fullmatch(r"-[A-Za-z]*i.*|--in-place.*", a) for a in args)
    if cmd in cfg["guards"]["writers"] or cmd in WRITERS or inplace:
        words, skip, script = [], False, inplace
        for a in args:
            if skip:
                skip = False  # the value of -e / -f is a script, not a file
            elif inplace and re.fullmatch(r"-[A-Za-z]*[ef]|--expression|--file", a):
                skip, script = True, False
            elif a.startswith(("--expression=", "--file=")):
                script = False
            elif not a.startswith("-"):
                if script:
                    script = False  # with no -e, the first word is the script
                else:
                    words.append(a)
        if cmd in COPIERS:  # only the destination of a copy is written; its sources are read
            tdir = [a for i, a in enumerate(args) if i and args[i - 1] in ("-t", "--target-directory")]
            tdir += [a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")]
            dest = tdir[-1:] or words[-1:]
            srcs = words if tdir else words[:-1]
            # into a directory a source lands as dir/basename; judge that too (a file dest just never matches)
            words = dest + [d.rstrip("/") + "/" + re.split(r"[\\/]", s.rstrip("/\\"))[-1] for d in dest for s in srcs]
        targets += words
    return targets


def writes(cfg, command, root, cwd):
    """Repo-relative paths the command writes, in order, each once."""
    out = []
    for seg in segments(command):
        if _cmd(seg[0]) == "cd" and seg[1:2] and not seg[1].startswith("-"):
            cwd = _join(cwd, seg[1])  # a `cd X &&` in the chain moves where later relative paths land
        for t in _targets(cfg, _cmd(seg[0]), seg[1:]):
            rel = _rel(t, root, cwd)
            if rel and not rel.startswith("..") and rel not in out:
                out.append(rel)
    return out
