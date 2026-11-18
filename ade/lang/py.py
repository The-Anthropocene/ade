# id: ade.lang.py
# what: Python reader: absolute and relative imports resolved to repo files, the deepest existing module winning; `pkgutil.iter_modules(pkg.__path__)` imports every module in pkg
# cmd: -
# reads: -
# writes: -
import ast

EXTS = (".py",)


def _module(name):
    parts = name[:-3].split("/") if name.endswith(".py") else name.split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def prepare(root, names):
    return {_module(n): n for n in names if n.endswith(".py")}


def _names(name, text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    pkg = _module(name) if name.endswith("__init__.py") else _module(name).rpartition(".")[0]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)] if pkg else []
                base = ".".join([*up, base] if base else up)
            out += [f"{base}.{a.name}" if base else a.name for a in node.names] + [base]
        elif isinstance(node, ast.Call) and _dotted(node.func).endswith("iter_modules") and node.args:
            # Discovery by folder (`iter_modules(ade.verbs.__path__)`) reaches every module there: a new verb file
            # has no import naming it, so without this its callers' tests are never picked.
            where = _dotted(node.args[0])
            if where == "__path__" or where.endswith(".__path__"):
                out.append((pkg if where == "__path__" else where[:-9]) + ".*")
    return out


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    return f"{_dotted(node.value)}.{node.attr}" if isinstance(node, ast.Attribute) else ""


def imports(name, text, by_module):
    deps = set()
    for imp in _names(name, text):
        if imp.endswith(".*"):
            deps.update(f for m, f in by_module.items() if m.startswith(imp[:-1]) and f != name)
            continue
        # The deepest existing module wins: `from ade import graph` points at ade/graph.py.
        while imp and imp not in by_module:
            imp = imp.rpartition(".")[0]
        if imp and by_module[imp] != name:
            deps.add(by_module[imp])
    return deps
