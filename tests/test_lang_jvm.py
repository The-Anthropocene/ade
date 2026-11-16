# id: tests.test_lang_jvm
# what: the JVM reader links imports to the file whose package and stem match under any source root, and wildcards only to mentioned stems
# cmd: -
# reads: -
# writes: temporary directories only
import tempfile
import unittest
from pathlib import Path

from ade.lang import jvm


class JvmReader(unittest.TestCase):
    def test_edges(self):
        # Guards the contract: named, static and Kotlin imports resolve by package line + stem across source
        # roots; wildcard and own-package reach only files whose stem is a whole word; unknown names are no edge.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            files = {"src/main/java/a/Foo.java": "package com.x;\n", "src/main/java/a/Bar.java": "package com.x;\n",
                     "src/main/java/a/Baz.java": "package com.x;\n", "src/main/kotlin/K.kt": "package com.k\n",
                     "src/test/java/FooTest.java": "package com.t;\n"}
            for n, t in files.items():
                (root / n).parent.mkdir(parents=True, exist_ok=True)
                (root / n).write_text(t)
            st = jvm.prepare(root, list(files))
            foo, bar, k = "src/main/java/a/Foo.java", "src/main/java/a/Bar.java", "src/main/kotlin/K.kt"
            t = "src/test/java/FooTest.java"
            for src in ("import com.x.Foo;", "import static com.x.Foo.bar;", "import com.x.Foo"):
                self.assertEqual(jvm.imports(t, f"package com.t;\n{src}\n", st), {foo})
            self.assertEqual(jvm.imports(t, "import com.k.K as Q\n", st), {k})
            self.assertEqual(jvm.imports(t, "import com.x.*;\nBar b; Barn c;", st), {bar})
            self.assertEqual(jvm.imports(foo, "package com.x;\nBar b;", st), {bar})
            self.assertEqual(jvm.imports(t, "import java.util.List;\nimport org.y.Foo;", st), set())


if __name__ == "__main__":
    unittest.main()
