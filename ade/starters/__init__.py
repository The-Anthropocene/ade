# id: ade.starters
# what: starter projects for `ade init` in an empty folder, one module per language, discovered from this folder
# cmd: -
# reads: ade/template.toml
# writes: -
import importlib
import pkgutil
import re

from ade import firstrun, host
from ade.config import TEMPLATE

DEFAULT = "python"


def names():
    return sorted(m.name for m in pkgutil.iter_modules(__path__))


def package_name(folder):
    # The folder name, made into an identifier both languages accept; "app" when nothing is left.
    name = re.sub(r"\W+", "_", folder.lower()).strip("_")
    return name if name and not name[0].isdigit() else "app"


def files(starter, folder):
    """Every file the starter writes, as {relative path: text}, ade.toml included."""
    mod = importlib.import_module(f"ade.starters.{starter}")
    pkg = package_name(folder)
    out = {k.format(pkg=pkg): v.format(pkg=pkg, name=folder) for k, v in mod.FILES.items()}
    toml = host.read(TEMPLATE)
    for old, new in mod.TOML.items():
        if old not in toml:
            raise ValueError(f"starter {starter}: {old!r} is not in the template")  # a kit defect, caught by its tests
        toml = toml.replace(old, new.replace("{pkg}", pkg))
    out["ade.toml"] = firstrun.with_defaults(toml)
    return out, {k: v.format(pkg=pkg) if isinstance(v, str) else [p.format(pkg=pkg) for p in v]
                 for k, v in mod.CARD.items()}
