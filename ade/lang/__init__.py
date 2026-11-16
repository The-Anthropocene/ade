# id: ade.lang
# what: one import reader per language, discovered from this folder; each exports EXTS, prepare(root, names), imports(name, text, state)
# cmd: -
# reads: -
# writes: -
import importlib
import pkgutil


def readers():
    # Adding a language is adding a file here; nothing else lists them.
    return {m.name: importlib.import_module(f"ade.lang.{m.name}") for m in pkgutil.iter_modules(__path__)}
