# id: tests.test_lang_go
# what: the Go reader links imports of module packages to their non-test files and nothing else
# cmd: -
# reads: -
# writes: temporary directories only
import tempfile
import unittest
from pathlib import Path

from ade.lang import go


class GoReader(unittest.TestCase):
    def test_edges(self):
        # Guards the contract: module-path imports (single, grouped, aliased) give every non-test file of that
        # folder only; stdlib gives none; a _test.go file reaches its own package's files.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            files = {"go.mod": "module example.com/app\n", "main.go": "", "util/a.go": "", "util/b.go": "",
                     "util/x_test.go": "", "util/sub/s.go": ""}
            for n, t in files.items():
                (root / n).parent.mkdir(parents=True, exist_ok=True)
                (root / n).write_text(t)
            st = go.prepare(root, [n for n in files if n.endswith(".go")])
            want = {"util/a.go", "util/b.go"}
            for src in ('import "example.com/app/util"', 'import u "example.com/app/util"',
                        'import (\n\t"fmt"\n\tu "example.com/app/util"\n)'):
                self.assertEqual(go.imports("main.go", f"package main\n{src}\n", st), want)
            self.assertEqual(go.imports("main.go", 'package main\nimport "fmt"\n', st), set())
            self.assertEqual(go.imports("util/x_test.go", "package util\n", st), want)


if __name__ == "__main__":
    unittest.main()
