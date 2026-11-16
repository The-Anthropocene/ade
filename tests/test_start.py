# id: tests.test_start
# what: `ade init` in an empty folder starts a project (repo, starter, passing test, first card); the newcomer rehearsal flies landing it
# cmd: python3 -m unittest tests/test_start.py
# reads: -
# writes: temporary directories only
from ade import starters  # noqa: F401  (imported so the graph records coverage)
from ade.starters import python, typescript  # noqa: F401
from ade.verbs import init  # noqa: F401
from tests.helpers import FixtureCase


class StartTest(FixtureCase):
    def test_starter_blesses_its_lib_folder_dry_run_writes_nothing_and_it_is_refused_where_files_exist(self):
        # Guards the contract: ade.toml's [graph] blessed glob matches the lib folder the starter creates.
        import fnmatch
        import tomllib
        for starter in ("python", "typescript"):
            folder = self.tmp / starter
            folder.mkdir()
            code, out = self.ade("init", "--starter", starter, cwd=folder)
            self.assertEqual(code, 0, out)
            blessed = tomllib.loads((folder / "ade.toml").read_text())["graph"]["blessed"]
            found = [p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()]
            self.assertTrue(any(fnmatch.fnmatch(f, blessed) for f in found), (starter, blessed))
        folder = self.tmp / "dry"
        folder.mkdir()
        code, out = self.ade("init", "--dry-run", "--starter", "typescript", cwd=folder)
        self.assertEqual(code, 0, out)
        self.assertIn("src/greet.ts", out)
        self.assertEqual(list(folder.iterdir()), [])
        code, out = self.ade("init", "--starter", "python")
        self.assertEqual(code, 2, out)
        self.assertIn("init.refused", out)
