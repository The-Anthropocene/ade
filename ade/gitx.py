# id: ade.gitx
# what: run git, turning a failure into a refusal that quotes git's own last line
# cmd: -
# reads: git repository
# writes: git repository
import os
import subprocess

from ade.refuse import refuse


def git(cwd, *args, check=True, input=None, env=None, timeout=None):
    # Bytes in and out: text mode on Windows would turn the input's LF into CRLF, which mktree reads as part of a name.
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, capture_output=True, input=None if input is None else input.encode(),
                           env={**os.environ, **env} if env else None, timeout=timeout)
    except FileNotFoundError:
        refuse("git.failed", "git is not on PATH", "install git or put it on PATH, then rerun the same ade command")
    r.stdout, r.stderr = (b.decode("utf-8", "replace") for b in (r.stdout, r.stderr))
    if check and r.returncode != 0:
        last = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:] or ["no output"]
        cure = "fix the repository state git names, then rerun the same ade command"
        if os.environ.get("CODEX_SANDBOX"):  # same wording as lane.sandboxed
            cure = ("this session runs in Codex's sandbox, which makes .git read-only; rerun from a plain terminal, "
                    "or start Codex with --sandbox danger-full-access")
        refuse("git.failed", f"git {args[0]} failed in {cwd}: {last[0]}", cure)
    return r.stdout.strip() if check else r
