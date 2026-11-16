# id: tests.test_lang_rs
# what: the Rust reader links mod declarations and crate/self/super use paths to repo module files, and tests/ files to lib.rs
# cmd: -
# reads: -
# writes: temporary directories only
import tempfile
import unittest
from pathlib import Path

from ade.lang import rs


class RustReader(unittest.TestCase):
    def test_edges(self):
        # Guards the contract: `mod x;` finds x.rs or x/mod.rs (nested under a non-mod file's folder), use paths
        # resolve to the deepest existing module, super/self are relative to the file, tests/ reach lib.rs, and
        # unresolvable paths (std, third-party) give no edge.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            names = ["Cargo.toml", "src/lib.rs", "src/a.rs", "src/a/b.rs", "src/c/mod.rs", "tests/t.rs"]
            for n in names:
                (root / n).parent.mkdir(parents=True, exist_ok=True)
                (root / n).write_text("")
            st = rs.prepare(root, names)
            self.assertEqual(rs.imports("src/lib.rs", "mod a;\nmod c;\n", st), {"src/a.rs", "src/c/mod.rs"})
            self.assertEqual(rs.imports("src/a.rs", "pub mod b;\n", st), {"src/a/b.rs"})
            self.assertEqual(rs.imports("src/c/mod.rs", "use crate::a::b::Thing;\n", st), {"src/a/b.rs"})
            self.assertEqual(rs.imports("src/c/mod.rs", "use crate::a::{b::X, Y};\n", st), {"src/a/b.rs", "src/a.rs"})
            self.assertEqual(rs.imports("src/a/b.rs", "use super::Q;\n", st), {"src/a.rs"})
            self.assertEqual(rs.imports("src/a.rs", "use self::b::Z;\n", st), {"src/a/b.rs"})
            self.assertEqual(rs.imports("tests/t.rs", "use std::fmt;\nuse serde::X;\n", st), {"src/lib.rs"})
            self.assertEqual(rs.imports("src/a.rs", "use std::fmt;\nuse serde::X;\n", st), set())


if __name__ == "__main__":
    unittest.main()
