# id: tests.test_lang_cs
# what: the C# reader links usings to files by namespace and whole-word stem, also the own namespace and its parents
# cmd: -
# reads: -
# writes: temporary directories only
import tempfile
import unittest
from pathlib import Path

from ade.lang import cs


class CsReader(unittest.TestCase):
    def test_edges(self):
        # Guards the contract: using/global using/static/alias resolve by namespace line (block or file-scoped)
        # and whole-word stem; own namespace and parents count; unknown names are no edge.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            files = {"src/Strings.cs": "namespace App.Util { class Strings {} }\n",
                     "src/Maths.cs": "namespace App.Util;\n", "src/Core.cs": "namespace App;\n",
                     "test/T.cs": "namespace App.Tests;\n"}
            for n, t in files.items():
                (root / n).parent.mkdir(parents=True, exist_ok=True)
                (root / n).write_text(t)
            st = cs.prepare(root, list(files))
            strings, core, t = "src/Strings.cs", "src/Core.cs", "test/T.cs"
            for src in ("using App.Util;", "global using App.Util;"):
                self.assertEqual(cs.imports("x/X.cs", f"{src}\nStrings s; Mathsy m;", st), {strings})
            for src in ("using static App.Util.Strings;", "using S = App.Util.Strings;"):
                self.assertEqual(cs.imports("x/X.cs", f"{src}\n", st), {strings})
            self.assertEqual(cs.imports(t, "namespace App.Tests;\nCore c; Maths m;", st), {core})
            self.assertEqual(cs.imports("x/X.cs", "using System;\nusing Other.Strings;", st), set())


if __name__ == "__main__":
    unittest.main()
