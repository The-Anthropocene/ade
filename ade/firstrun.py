# id: ade.firstrun
# what: the machine defaults file path, and the first-run welcome with engine checks (found or not found, never an install command)
# cmd: ade
# reads: PATH, APPDATA, XDG_CONFIG_HOME
# writes: -
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

from ade import host, parts
from ade.refuse import refuse

TEMPLATE = Path(__file__).with_name("template.toml")  # config.py cannot be imported here: it imports telemetry, which imports this
ENGINES = ("claude", "codex", "opencode", "pi")


def config_dir():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "ade"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ade"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ade"


def defaults_path():
    return config_dir() / "defaults.toml"


def machine_parts():
    """The [parts] table of the machine defaults file as (off ids, overrides), or None without one."""
    f = defaults_path()
    try:
        table = tomllib.loads(host.read(f)).get("parts") if f.is_file() else None
    except tomllib.TOMLDecodeError as e:
        refuse("defaults.broken", f"the machine defaults file {f} is not valid TOML ({e})",
               f"fix or delete {f}, then run `ade setup` outside a project to make it again")
    if table is None:
        return None
    return set(table.get("off", [])), table.get("override", {})


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def with_defaults(toml):
    """A first ade.toml's text started from the machine defaults' parts; unchanged when there are none."""
    got = machine_parts()
    toml = toml if got is None else parts.rewrite(toml, *got)
    if is_root():
        print('lane_mode = "auto": running as root, Claude Code refuses bypassPermissions; auto lets lanes run any routine command and refuses risky ones')
        toml = toml.replace('lane_mode = "bypassPermissions"', 'lane_mode = "auto"', 1)
    return chosen(toml)


HEADER = ("# ade.toml holds only what `ade init` chose for this project. Every other key takes the kit's default;\n"
          "# `ade config` shows every key and what it does. Add a key here to change it.\n")


def chosen(toml):
    """A first ade.toml: the header and the keys of TOML that differ from the kit template, which config.load_at layers underneath."""
    def walk(mine, base, path):
        flat = [(k, v) for k, v in mine.items() if not isinstance(v, dict) and base.get(k) != v]
        subs = [(k, v) for k, v in mine.items() if isinstance(v, dict)]
        out = ([f"[{'.'.join(path)}]"] + [f"{k} = {json.dumps(v)}" for k, v in flat] + [""]) if flat and path else []
        for k, v in subs:
            out += walk(v, base.get(k, {}), path + [k])
        return out
    body = walk(tomllib.loads(toml), tomllib.loads(host.read(TEMPLATE)), [])
    return HEADER + "\n" + "\n".join(body).rstrip() + "\n"


def welcome():
    """Lines to print above the verb list, or [] once the machine has its defaults file."""
    if defaults_path().exists():
        return []
    out = ["Welcome. This machine has no ade defaults yet; `ade setup` makes them.", "", "Engines on your PATH:"]
    pad = max(map(len, ENGINES)) + 2
    for name in ENGINES:
        out.append(f"  {name:<{pad}}" + ("found" if shutil.which(name) else "not found"))
    return out + [""]
