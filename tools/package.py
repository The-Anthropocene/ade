# id: tools.package
# what: build the portable downloads: a standalone Python with ade inside, a launcher, and MinGit on Windows, per platform
# cmd: python3 tools/package.py [--platform linux|macos-arm64|macos-x64|windows ...] [--out dist] [--cache DIR]
# reads: ade/, pyproject.toml, tools/notices/; downloads the pinned Python and MinGit archives and checks their SHA-256
# writes: OUT/atlas-ade-<version>-<platform>.tar.gz (.zip on Windows), and the downloads under --cache
import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Pinned by release and hash, never "latest". To move a pin: take a release at least two weeks old, read its
# SHA256SUMS (python-build-standalone) or hash the asset (MinGit), and change the three values together.
PBS = "https://github.com/astral-sh/python-build-standalone/releases/download/20261009/"
PYTHON = {
    "linux": ("cpython-3.13.16+20261009-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
              "24801ea31c7d406d559a17e2226325c723d50c56900b9c0da7b62adaae2affc9"),
    "macos-arm64": ("cpython-3.13.16+20261009-aarch64-apple-darwin-install_only_stripped.tar.gz",
                    "11785155d0f70e634178219c20925d8f20be00a3da4684fc4f5019dd15602ef7"),
    "macos-x64": ("cpython-3.13.16+20261009-x86_64-apple-darwin-install_only_stripped.tar.gz",
                  "bd085c51e960f00ee33b630e2b13ac90fe90cdc3da25a0f0bc0719b1294920bf"),
    "windows": ("cpython-3.13.16+20261009-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
                "3a7adfb69e3ff02a41ed38ac637808532b438ab3c8619af46ca5dbec5c1f5190"),
}
MINGIT = ("https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.1/MinGit-2.56.0-64-bit.zip",
          "064b440ff870ed5198527e8f3a92cdf5bd2fd0fedf5e718af95e3fdaddeff718")

# The license texts of the libraries python-build-standalone compiles into Python. Its install_only archives leave
# them out, so they are kept here: when PBS moves, replace tools/notices/python/ with python/licenses/ from the same
# release's *-full.tar.zst (every platform's set is the same text). Everything else ships its own license files,
# which build() finds by name and copies into THIRD_PARTY_NOTICES.txt whole.
NOTICES = ROOT / "tools" / "notices"
CREDITS = {
    "all": [
        ("CPython 3.13.16", "PSF License Version 2", "https://www.python.org (source: https://www.python.org/downloads/source/)"),
        ("python-build-standalone 20261009", "MPL-2.0 (its build scripts; what it builds keeps the licenses below)",
         "https://github.com/astral-sh/python-build-standalone"),
        ("Libraries compiled into that Python: OpenSSL, SQLite, zlib, bzip2, xz (liblzma), libffi, mpdecimal, expat, "
         "ncurses, libedit, libuuid, Tcl/Tk, Tix, Berkeley DB, libX11, libXau, libxcb (each platform uses a subset)",
         "each its own; texts below", "listed by python-build-standalone"),
        ("pip 26.2.1 and the libraries it vendors", "MIT and others; texts below", "https://pip.pypa.io"),
    ],
    "windows": [
        ("MinGit 2.56.0 (Git for Windows), with the libraries it bundles: curl, OpenSSL, OpenSSH, PCRE2, zlib, zstd, "
         "libiconv, gettext, nghttp2, libssh2, Git Credential Manager and others", "Git: GPL-2.0; the rest each its own; "
         "texts below", "https://gitforwindows.org\n"
         "  Source: an archive of Git for Windows tag v2.56.0.windows.1 is published beside every Atlas ADE release on\n"
         "  GitHub (github.com/The-Anthropocene/ade/releases). For three years from this release we also send it to anyone who asks at\n"
         "  support@atlas-ade.dev. Upstream: https://github.com/git-for-windows/git/tree/v2.56.0.windows.1\n"
         "  and https://github.com/msys2/MINGW-packages"),
    ],
}
LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "RUNTIME.LIBRARY.EXCEPTION")

# Each launcher runs ade on the Python beside it, never the system's (-E and -s ignore PYTHON* variables and the
# user's site-packages), and sets ADE_LAUNCHER so hooks and `ade link` call the launcher, not the Python inside.
SH = '''#!/bin/sh
# Atlas ADE launcher: runs ade on the Python bundled beside it. `./ade link` puts a plain `ade` on your PATH.
here=$(cd "$(dirname "$0")" && pwd -P)
ADE_LAUNCHER="$here/ade" exec "$here/python/bin/python3" -E -s -m ade "$@"
'''
CMD = '''@echo off
rem Atlas ADE launcher: runs ade on the Python bundled beside it. "ade link" puts a plain ade on your PATH.
setlocal
set "ADE_LAUNCHER=%~f0"
rem The bundled git comes last, so a git you installed yourself still wins.
set "PATH=%PATH%;%~dp0git\\cmd"
"%~dp0python\\python.exe" -E -s -m ade %*
'''
INSTALL = '''Atlas ADE {version} for {platform}

Everything ade needs is in this folder: its own Python{git}. Nothing is installed on your system.{needs}

1. Unpack this folder wherever you keep tools (moving it later only needs step 3 again).
2. Check it runs:            {run} --version
3. Make a plain `ade` work:  {run} link
   That writes one small launcher into a folder of yours already on PATH, or prints an alias line to add.
4. In a new terminal:        mkdir my-project && cd my-project && ade init && ade loop c1

Running as root (containers, cloud VMs)? Claude Code refuses bypassPermissions as root, so lanes need
[permissions] lane_mode = "auto" in ade.toml. If ade init has not already set it, ask the person to
set it (ade.toml is protected, so an agent cannot), then run ade init again.

If a setting in ade.toml needs changing, an agent should name the key and value to the person.

You also need one engine installed and signed in with your own account: Claude Code, Codex, OpenCode or Pi.
ADE drives it and never installs it. The TypeScript starter needs Node 22.6 or newer. Nothing leaves your machine through ade; the engine you use
keeps its own telemetry under its own settings.

LICENSE is Atlas ADE's own licence. THIRD_PARTY_NOTICES.txt names the open-source software in this folder
and holds its licenses.
{extra}'''
README = '''Atlas ADE, the meta-harness for your agentic workflows.

This folder is the whole tool: ade, with its own Python inside. INSTALL.txt has the four steps to get a plain
`ade` command. Then run `ade` alone to see every verb, and `ade init` in a project folder to start.

Docs: https://atlas-ade.dev/docs/
Licence: LICENSE. Open-source software in this folder: THIRD_PARTY_NOTICES.txt.
'''
MAC = '''
On macOS, a download from a browser is marked as quarantined and the bundled Python may be blocked. Clear the
mark once, from the folder above this one:   xattr -dr com.apple.quarantine atlas-ade-*
'''


def fetch(url, sha, cache):
    """The file at url, from cache when its hash matches, else downloaded; refused unless the SHA-256 matches."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / url.rsplit("/", 1)[1]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        with urllib.request.urlopen(url, timeout=300) as r:
            path.write_bytes(r.read())
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != sha:
        sys.exit(f"{path.name}: SHA-256 {got} is not the pinned {sha}; nothing was built")
    return path


def notices(stage, platform, version):
    """THIRD_PARTY_NOTICES.txt: who made what this download carries, then every license text, whole and unchanged."""
    out = [f"Third-party notices for Atlas ADE {version} ({platform})\n\n"
           "This download carries the open-source software below, unmodified. Each keeps its own license; the full\n"
           "texts follow. Claude Code, Codex and Orca (https://github.com/stablyai/orca, MIT) are not in it: ADE\n"
           "drives the copies you install yourself, and thanks their makers all the same.\n"]
    for name, lic, where in CREDITS["all"] + CREDITS.get(platform, []):
        out.append(f"\n* {name}\n  License: {lic}\n  {where}\n")
    texts = [(f"Libraries compiled into Python: {f.name}", f) for f in sorted((NOTICES / "python").glob("LICENSE.*.txt"))]
    texts.append(("python-build-standalone", NOTICES / "python-build-standalone.LICENSE.txt"))
    shipped = sorted(f for f in stage.rglob("*") if f.is_file() and f.suffix != ".py" and f != stage / "LICENSE"  # the kit's own
                     and "dist-info/licenses/src" not in f.as_posix()  # pip's second copy of its vendored texts
                     and (f.name.upper().startswith(LICENSE_NAMES) or f.parent.name == "licenses"
                          or "share/licenses" in f.as_posix()))
    texts += [(f"{f.relative_to(stage).as_posix()} (in this folder)", f) for f in shipped]
    if len(texts) < 20 or not all(f.is_file() for _, f in texts):
        sys.exit("THIRD_PARTY_NOTICES.txt is missing license texts (tools/notices/ or the downloads); nothing was built")
    for title, f in texts:
        out.append(f"\n\n{'=' * 78}\n{title}\n{'=' * 78}\n\n" + f.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n"))
    return "".join(out)


def install_ade(python_dir, version):
    """Put the ade package into the bundled Python's site-packages, with the metadata `ade --version` reads."""
    site = next(python_dir.glob("lib/python3.*/site-packages"), None) or python_dir / "Lib" / "site-packages"
    shutil.copytree(ROOT / "ade", site / "ade", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    info = site / f"atlas_ade-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: atlas-ade\nVersion: {version}\n")
    (info / "INSTALLER").write_text("tools/package.py\n")
    (info / "RECORD").write_text("")
    return site


def build(platform, out, cache, version):
    name = f"atlas-ade-{version}-{platform}"
    stage = out / "stage" / name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    with tarfile.open(fetch(PBS + PYTHON[platform][0], PYTHON[platform][1], cache)) as t:
        t.extractall(stage, filter="data")  # the archive's top folder is python/
    site = install_ade(stage / "python", version)
    # What this kit ships under ade/, so `ade link` can later tell which files were edited in this folder.
    (stage / "kit-files.json").write_text(json.dumps({f.relative_to(site).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                                                      for f in sorted((site / "ade").rglob("*")) if f.is_file()}))
    win = platform == "windows"
    if win:
        with zipfile.ZipFile(fetch(*MINGIT, cache)) as z:
            z.extractall(stage / "git")
        (stage / "ade.cmd").write_bytes(CMD.replace("\n", "\r\n").encode())
    else:
        (stage / "ade").write_text(SH)
        (stage / "ade").chmod(0o755)
    (stage / "README.md").write_bytes((README.replace("\n", "\r\n") if win else README).encode())
    shutil.copy(ROOT / "LICENSE", stage / "LICENSE")  # the kit's own licence; third parties are in THIRD_PARTY_NOTICES.txt
    run = r".\ade.cmd" if win else "./ade"
    needs = "" if win else ("\nIt uses the git you already have" + (" (typing `git` once in a terminal offers to install it)"
                                                                    if platform.startswith("macos") else "") + ".")
    text = INSTALL.format(version=version, platform=platform, run=run, git=" and git (MinGit)" if win else "", needs=needs,
                          extra=MAC if platform.startswith("macos") else "")
    (stage / "INSTALL.txt").write_bytes((text.replace("\n", "\r\n") if win else text).encode())
    text = notices(stage, platform, version)
    (stage / "THIRD_PARTY_NOTICES.txt").write_bytes((text.replace("\n", "\r\n") if win else text).encode())
    out.mkdir(parents=True, exist_ok=True)
    if win:
        target = out / f"{name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for f in sorted(stage.rglob("*")):
                z.write(f, f.relative_to(stage.parent))
    else:
        target = out / f"{name}.tar.gz"
        with tarfile.open(target, "w:gz") as t:
            t.add(stage, arcname=name)  # symlinks and the launcher's execute bit are kept
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    (out / f"{target.name}.sha256").write_text(f"{sha}  {target.name}\n")
    print(f"{target}  {target.stat().st_size // 1_000_000} MB  sha256 {sha}")
    return target


def main():
    p = argparse.ArgumentParser(description="Build Atlas ADE's portable downloads.")
    p.add_argument("--platform", action="append", choices=sorted(PYTHON), help="repeatable; default every platform")
    p.add_argument("--out", type=Path, default=ROOT / "dist")
    p.add_argument("--cache", type=Path, default=ROOT / "dist" / "cache")
    args = p.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    for platform in args.platform or sorted(PYTHON):
        build(platform, args.out.resolve(), args.cache.resolve(), version)
    shutil.rmtree(args.out.resolve() / "stage", ignore_errors=True)


if __name__ == "__main__":
    main()
