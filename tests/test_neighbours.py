# id: tests.test_neighbours
# what: neighbour check: kit code never touches tmux, launchd, brew, a bare pip install or the user's ~/.claude/settings.json
# cmd: python3 -m unittest tests/test_neighbours.py
# reads: ade/**/*.py, bin/ade
# writes: nothing
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RULES = [
    ("tmux", re.compile(r"tmux")),
    ("/Volumes/", re.compile(r"/Volumes/")),
    ("launchctl/launchd", re.compile(r"launchctl|launchd")),
    ("brew install", re.compile(r"brew\s+install")),
    ("pip install outside a venv or --target",
     re.compile(r"pip\s+install(?!.*(--target|venv))")),
    ("write to ~/.claude/settings.json",
     re.compile(r"~/\.claude/settings\.json|home\(\).*\.claude.*settings\.json")),
]

# Lines that only name a rule: exact (file, substring) pairs.
ALLOWED = [
    ("ade/verbs/watch.py", "no tmux"),
    ("ade/verbs/session.py", "no tmux"),
]


def _files():
    return sorted((ROOT / "ade").rglob("*.py")) + [ROOT / "bin" / "ade"]


class Neighbours(unittest.TestCase):
    # Guards the shared-machine rule (ARCHITECTURE.md 8.2, test 5): the kit never reaches into its neighbours.
    def test_no_neighbour_contact(self):
        bad = []
        for path in _files():
            rel = path.relative_to(ROOT).as_posix()
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(f == rel and s in line for f, s in ALLOWED):
                    continue
                for name, rx in RULES:
                    if rx.search(line):
                        bad.append(f"{rel}:{n}: {name}: {line.strip()}")
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
