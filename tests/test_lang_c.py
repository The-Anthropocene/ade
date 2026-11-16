# id: tests.test_lang_c
# what: the C/C++ reader resolves quoted includes folder-first, angle includes via include/ only, and headers to same-stem sources
# cmd: -
# reads: -
# writes: temporary directories only
import tempfile
import unittest
from pathlib import Path

from ade.lang import c


class CReader(unittest.TestCase):
    def test_edges(self):
        # Guards the contract: "x.h" tries own folder, root, include/ folders; <x.h> only include/ folders
        # (else system header, no edge); a header imports its same-stem source.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            files = ["src/a.c", "src/util.h", "src/util.c", "src/b.cpp", "root.h", "lib/include/inc.h",
                     "lib/include/sub/deep.h"]
            for n in files:
                (root / n).parent.mkdir(parents=True, exist_ok=True)
                (root / n).write_text("")
            st = c.prepare(root, files)
            self.assertEqual(c.imports("src/a.c", '#include "util.h"\n', st), {"src/util.h"})
            self.assertEqual(c.imports("src/util.h", "", st), {"src/util.c"})
            self.assertEqual(c.imports("src/a.c", '#include "root.h"\n#include "inc.h"\n', st),
                             {"root.h", "lib/include/inc.h"})
            self.assertEqual(c.imports("src/b.cpp", "#include <inc.h>\n#  include <sub/deep.h>\n", st),
                             {"lib/include/inc.h", "lib/include/sub/deep.h"})
            self.assertEqual(c.imports("src/b.cpp", "#include <root.h>\n#include <stdio.h>\n", st), set())


if __name__ == "__main__":
    unittest.main()
