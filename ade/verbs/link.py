# id: ade.verbs.link
# what: make a plain `ade` work in new shells: one small launcher named ade in a per-user folder already on PATH
# cmd: ade link [--dry-run] [--force]
# reads: PATH, the running ade's location
# writes: ~/.local/bin/ade or ~/bin/ade (a sh script), or ade.cmd there or in WindowsApps on Windows; nothing else
import hashlib
from importlib import metadata
import json
import os
import re
import shutil
from pathlib import Path

from ade import install
from ade.refuse import refuse

MARK = "ade link"  # in every launcher this verb writes, so a rerun can tell its own file from someone else's


def add(p):
    p.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    p.add_argument("--force", action="store_true", help="repoint the launcher even when its kit is not older than this one")


def _folders():
    # Only per-user folders, and only ones already on PATH: a venv's bin on PATH would put its python3 ahead of the
    # system's, so the install folder itself is never offered, and no PATH or shell profile is ever edited.
    on = {os.path.normcase(os.path.abspath(d)) for d in os.environ.get("PATH", "").split(os.pathsep) if d}
    home = Path.home()
    mine = [home / ".local" / "bin", home / "bin"]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        mine.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WindowsApps")
    return [d for d in mine if os.path.normcase(os.path.abspath(d)) in on and d.is_dir()]


def _ours(p):
    return p.is_file() and p.stat().st_size < 1000 and MARK in p.read_text(errors="replace")


def _edited(dst, body):
    # Moving off an older kit folder: name the files in it whose hash is no longer the one its kit-files.json shipped.
    # Warn only; nothing is copied, merged or patched.
    old = re.search(r'"([^"]+)"', dst.read_text(errors="replace")) if dst.exists() and _ours(dst) else None
    kit = Path(old.group(1)).parent if old and old.group(1) not in body else None
    try:
        shipped = json.loads((kit / "kit-files.json").read_text()) if kit else {}
        site = next(kit.glob("python/lib/python3.*/site-packages"), None) or kit / "python" / "Lib" / "site-packages"
        gone = [f for f, h in sorted(shipped.items())
                if not (site / f).is_file() or hashlib.sha256((site / f).read_bytes()).hexdigest() != h]
    except (OSError, ValueError, AttributeError):
        return
    if gone:
        print(f"you changed these in the old kit ({kit}); the new kit does not have your changes:")
        print("".join(f"  {f}\n" for f in gone), end="")


def _nums(v):
    return tuple(int(n) for n in re.findall(r"\d+", v))


def _upgrade(old):
    # True when the launcher's target is gone, or is a packaged kit older than the running one (unreadable versions: upgrade wins).
    if not Path(old).is_file():
        return True
    kit = Path(old).parent
    sites = list(kit.glob("python/lib/python3.*/site-packages")) + [kit / "python" / "Lib" / "site-packages"]
    found = [d.name[len("atlas_ade-"):-len(".dist-info")] for s in sites for d in s.glob("atlas_ade-*.dist-info")]
    if not found:
        return False
    here = install.kit().split()[0]  # as cli._version reads it: the checkout's pyproject, else the installed metadata
    if here == "unknown":
        try:
            here = metadata.version("atlas-ade")
        except metadata.PackageNotFoundError:
            return True
    return _nums(found[0]) < _nums(here)


def run(args, cwd):
    found = shutil.which("ade")
    if found and not _ours(Path(found)):
        print(f"ade is already on PATH: {found}")
        return 0
    cmd = install.launcher()
    nt = os.name == "nt"
    body = (f"@rem {MARK}: runs the ade install kept off PATH\r\n@{cmd} %*\r\n" if nt else
            f"#!/bin/sh\n# {MARK}: runs the ade install kept off PATH\nexec {cmd} \"$@\"\n")
    folders = _folders()
    open_ = [d for d in folders if os.access(d, os.W_OK)]
    if folders and not open_:
        # A sandbox (Codex's default workspace-write) makes a real folder read-only; say so rather than "none on PATH".
        print(f"{folders[0]} exists and is on PATH but isn't writable, so nothing was written. If an agent sandbox "
              "is the cause, approve this step or run `ade link` yourself in a normal shell.")
        return 0
    folders = open_
    if not folders:
        print("no per-user folder on PATH to put a launcher in, so nothing was written. Either add one yourself:")
        print("  " + ("PowerShell:  Set-Alias ade " + cmd + "   (put it in $PROFILE to keep it)" if nt else
                      f"alias ade='{cmd}'   (put it in ~/.zshrc or ~/.bashrc to keep it)"))
        print("or create ~/.local/bin, put it on PATH, and run this again.")
        return 0
    dst = folders[0] / ("ade.cmd" if nt else "ade")
    if dst.exists() and not _ours(dst):
        refuse("link.taken", f"{dst} exists and ade link did not write it",
               f"move or delete {dst} if it is an old ade, then run `ade link` again")
    _edited(dst, body)
    old = re.search(r'"([^"]+)"', dst.read_text(errors="replace")) if dst.exists() else None
    held = bool(old and old.group(1) not in body and not args.force and not _upgrade(old.group(1)))
    note = (f"{dst} runs {old.group(1)} now; it would run {cmd}. Not repointed; rerun `ade link --force` to repoint it.\n"
            if held else "")
    if args.dry_run:
        print(f"would write {dst}:\n{body}{note}", end="")
        return 0
    if held:
        print(note, end="")
        return 1
    dst.write_text(body, newline="")
    if not nt:
        dst.chmod(0o755)
    print(f"wrote {dst}; `ade` now runs {cmd} in any new shell (delete that one file to undo)")
    return 0
