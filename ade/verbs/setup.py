# id: ade.verbs.setup
# what: a terminal panel to switch the kit's parts on and off; shows the ade.toml diff first, saves through parts.rewrite; a numbered list when there is no terminal or NO_COLOR is set
# cmd: ade setup
# reads: ade/parts.toml, ade.toml [parts]
# writes: ade.toml [parts], or the machine defaults file outside a project
import os
import sys

from ade import config, firstrun, host, parts
from ade.setup import Panel

KEYS = {"\x1b[A": "up", "\x1b[B": "down", "k": "up", "j": "down", " ": "toggle", "\t": "cycle", "\r": "save", "\n": "save", "q": "quit", "\x03": "quit"}
WIN = {"H": "up", "P": "down"}  # the byte after msvcrt's \x00 or \xe0 prefix


def add(p):
    pass


def _key():
    """One key press as a name from KEYS' values, or '' for one the panel ignores."""
    if os.name == "nt":
        import msvcrt
        c = msvcrt.getwch()
        return WIN.get(msvcrt.getwch(), "") if c in "\x00\xe0" else KEYS.get(c, "")
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        c = os.read(fd, 1).decode("utf-8", "replace")
        while c.startswith("\x1b") and len(c) < 3 and select.select([fd], [], [], 0.05)[0]:
            c += os.read(fd, 1).decode("utf-8", "replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return KEYS.get(c, "")


def _draw(panel, last):
    lines = ["ade setup: up/down move, space switches, tab next group, enter reviews and saves, q quits", ""]
    for i, pid in enumerate(panel.ids):
        c = panel.cat[pid]
        row = f"{'>' if i == panel.at else ' '} [{' ' if pid in panel.off else 'x'}] {pid:10} {c['group']:8} {c['what']}"
        lines.append(f"\033[7m{row}\033[0m" if i == panel.at else row)
    lines += [""] + [f"warning: {w}" for w in panel.warnings()]
    print((f"\033[{last}A\033[J" if last else "") + "\n".join(lines), flush=True)
    return len(lines)


def _ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def _confirm(panel, text):
    diff = panel.diff(text)
    print(f"\n{diff or 'no change to ade.toml'}")
    return bool(diff) and _ask("save? [y/N] ").strip().lower() == "y"


def _panel(panel, text):
    shown = _draw(panel, 0)
    while True:
        key = _key()
        if key in ("up", "down"):
            panel.move(-1 if key == "up" else 1)
        elif key == "toggle":
            panel.toggle()
        elif key == "cycle":
            panel.cycle()
        elif key == "quit":
            return False
        elif key == "save":
            return _confirm(panel, text)
        shown = _draw(panel, shown)


def _numbered(panel, text):
    while True:
        for n, pid in enumerate(panel.ids, 1):
            print(f"{n:2}  {'off' if pid in panel.off else 'on '}  {pid:10} {panel.cat[pid]['what']}")
        for w in panel.warnings():
            print(f"warning: {w}")
        picks = _ask("numbers to switch, blank to review: ").split()
        if not picks:
            return _confirm(panel, text)
        for n in picks:
            if n.isdigit() and 0 < int(n) <= len(panel.ids):
                panel.toggle(panel.ids[int(n) - 1])


def run(args, cwd):
    root = config.main_worktree(cwd)
    if root is None or not (root / "ade.toml").is_file():
        f = firstrun.defaults_path()
        off, why = firstrun.machine_parts() or (set(), {})
        print(f"not in an ade project: editing the machine defaults ({f})")
    else:
        ctx = config.load(cwd)
        f = ctx.root / "ade.toml"
        off, why = parts.off(ctx.cfg), ctx.cfg["parts"]["override"]
    text = host.read(f) if f.is_file() else ""
    panel = Panel(parts.catalogue(), off, why)
    fancy = sys.stdin.isatty() and sys.stdout.isatty() and "NO_COLOR" not in os.environ and (os.name != "nt" or "WT_SESSION" in os.environ)
    if (_panel if fancy else _numbered)(panel, text):
        f.parent.mkdir(parents=True, exist_ok=True)
        host.write(f, panel.new_text(text).lstrip("\n"))
        print(f"wrote {f.name}")
    else:
        print("nothing saved")
    return 0
