# id: tests.test_graph
# what: the Python reader's folder discovery, the TypeScript reader on a fixture (aliases through extends, index files, .js names for .ts, require, import()), docs edges, test commands
# cmd: python3 -m unittest tests/test_graph.py
# reads: tests/fixture_ts, tests/fixture_repo
# writes: temporary directories only
import pathlib
import unittest
from unittest import mock

from ade import config, graph, host
from ade.lang import py, ts  # noqa: F401  (imported so the graph records coverage)
from ade.stages import test as test_stage
from tests.helpers import FIXTURE, FixtureCase


class TypeScriptGraphTest(FixtureCase):
    fixture = FIXTURE.with_name("fixture_ts")

    def test_imports_resolve_like_typescript(self):
        g = graph.build(self.repo, config.load_at(self.repo).cfg)
        app = g["files"]["src/app.tsx"]
        self.assertEqual(app["lang"], "ts")
        self.assertEqual(app["imports"], ["scripts/helper.cjs", "scripts/joined.cjs", "scripts/rooted.cjs", "src/late.ts", "src/lib/index.ts", "src/util.ts"])
        self.assertEqual(g["callers"]["data/cfg.json"], ["src/app.tsx"])  # a literal json require is a data edge
        self.assertEqual(g["files"]["src/lib/index.ts"]["imports"], ["src/lib/math.ts"])
        self.assertEqual(g["tests"]["src/lib/math.ts"], ["src/app.test.ts"])  # through the alias and the index
        self.assertEqual(g["files"]["src/lib/math.ts"]["header"]["id"], "src.lib.math")
        self.assertEqual(g["docs"], {"src/lib/math.ts": ["docs/notes.md"], "src/util.ts": ["docs/notes.md"]})


class FolderDiscoveryTest(unittest.TestCase):
    # Guards the P1 miss (28 Oct): a module found by `iter_modules(pkg.__path__)` is imported by the finder, so a new
    # file there reaches the finder's tests even though no import statement names it.
    def test_iter_modules_imports_every_module_in_the_package(self):
        mods = {"app.verbs": "app/verbs/__init__.py", "app.verbs.new": "app/verbs/new.py", "app.cli": "app/cli.py",
                "app.lang": "app/lang/__init__.py", "app.lang.py": "app/lang/py.py"}
        cli = "import pkgutil\nimport app.verbs\nfor m in pkgutil.iter_modules(app.verbs.__path__): pass\n"
        self.assertIn("app/verbs/new.py", py.imports("app/cli.py", cli, mods))
        self.assertIn("app/lang/py.py", py.imports("app/lang/__init__.py", "from pkgutil import iter_modules\n"
                                                   "iter_modules(__path__)\n", mods))


class DataEdgeTest(FixtureCase):
    def test_a_data_file_named_in_a_header_or_matched_by_its_glob_belongs_to_its_reader(self):
        # Guards the edge no import makes: a card on the data file and a card on its reader never fly together,
        # and the data file selects its reader's tests, not the critical set (ade rehearse clash-through-data flies it).
        # Guards the glob reading of reads/writes too: each matched data file gets the header's file as a caller.
        (self.repo / "data").mkdir()
        for name in ("limits.json", "a+b [1] - c.json", "a.json"):
            (self.repo / "data" / name).write_text("{}\n")
        (self.repo / "data" / "run.sh").write_text("true\n")
        calc = self.repo / "app" / "calc.py"
        plain = calc.read_text()
        for reads in ("data/limits.json, data/a+b [1] - c.json", "data/*.json, run:data/*.sh"):
            calc.write_text(plain.replace("# reads: -", "# reads: " + reads, 1))
            cfg = config.load_at(self.repo).cfg
            g = graph.build(self.repo, cfg)
            # Paths split on commas, not whitespace: a space, '+' or brackets in a name stays one edge.
            self.assertIn("app/calc.py", g["callers"]["data/a+b [1] - c.json"])
            self.assertTrue(graph.clash(graph.reach(g, ["data/limits.json"]), graph.reach(g, ["app/calc.py"])))
            self.assertEqual(graph.select_tests(g, ["data/limits.json"], cfg), graph.select_tests(g, ["app/calc.py"], cfg))
        self.assertIn("app/calc.py", g["callers"]["data/a.json"])
        # run:PATH makes the declarer a caller of the target, yet is no read input for co-moves.
        self.assertIn("app/calc.py", g["callers"]["data/run.sh"])
        self.assertEqual(graph.paths(g["files"]["app/calc.py"]["header"], "reads"), ["data/*.json"])
        self.assertEqual(graph.select_tests(g, ["data/a.json"], cfg), graph.select_tests(g, ["app/calc.py"], cfg))


class CoMoveTest(FixtureCase):
    def test_a_generators_header_makes_the_read_files_brief_name_the_written_file(self):
        # Guards the co-move edge: a code file whose header reads X and writes Y tells X's brief that Y is generated from it.
        (self.repo / "brief.md").write_text("x\n")
        calc = self.repo / "app" / "calc.py"
        calc.write_text(calc.read_text().replace("# reads: -", "# reads: brief.md", 1).replace("# writes: -", "# writes: out.txt", 1))
        g = graph.build(self.repo, config.load_at(self.repo).cfg)
        self.assertIn(("out.txt", "app/calc.py"), graph.comoves(g, "brief.md"))
        self.assertEqual(graph.writers(g, "out.txt"), ["app/calc.py"])  # guards the writers side of the same edge
        generator = calc.read_text()
        # Guards the in-place edge: a tool that reads and writes the same file is named; one that only generates it is not.
        calc.write_text(generator.replace("# writes: out.txt", "# writes: out.txt, brief.md", 1))
        g2 = graph.build(self.repo, config.load_at(self.repo).cfg)
        self.assertEqual(graph.rewriters(g2, "brief.md"), ["app/calc.py"])
        self.assertEqual(graph.rewriters(g2, "out.txt"), [])
        calc.write_text(generator)
        # Also guards that a header missing only writes: counts as '-' (its reads make edges), and that any other
        # partial header is reported by key rather than dropped.
        # (A data file, not brief.md: markdown is no_review, so it never gets callers.)
        (self.repo / "limits.json").write_text("{}\n")
        calc.write_text("\n".join(l.replace("# reads: brief.md", "# reads: limits.json") for l in calc.read_text().splitlines()
                                  if not l.startswith("# writes:")) + "\n")
        g = graph.build(self.repo, config.load_at(self.repo).cfg)
        self.assertEqual(g["files"]["app/calc.py"]["header"]["writes"], "-")
        self.assertIn("app/calc.py", g["callers"]["limits.json"])
        calc.write_text(calc.read_text().replace("# cmd:", "# x:", 1))
        g = graph.build(self.repo, config.load_at(self.repo).cfg)
        lacks = g["files"]["app/calc.py"]["lacks"]
        self.assertTrue(lacks)
        # Also guards that the partial-header report, like the missing-header line, prints only where require has "header".
        (self.repo / "ade.toml").write_text('[graph]\nrequire = ["tests"]\n')  # without a config graph is refused, which would pass vacuously
        code, out = self.ade("graph")
        self.assertEqual(code, 0)
        self.assertNotIn("header lacks", out)
        (self.repo / "ade.toml").write_text('[graph]\nrequire = ["header"]\n')
        self.assertIn(f"app/calc.py: header lacks {', '.join(lacks)}", self.ade("graph")[1])
        # Also guards that card add prints the same couplings for a named path, and nothing for one without.
        calc.write_text(generator)
        self.ade("init"), self.ade("graph")
        self.assertIn("co-moves: brief.md -> out.txt", self.ade("card", "add", "t", "--path", "brief.md", "--done", "d")[1])
        self.assertNotIn("co-moves", self.ade("card", "add", "u", "--path", "app/calc.py", "--done", "d")[1])


class TestCommandsTest(FixtureCase):
    def cfg(self, **tests):
        cfg = config.load_at(self.repo).cfg
        cfg["tests"] = {**cfg["tests"], **tests}
        return cfg

    def test_per_file_and_always_commands(self):
        g = graph.build(self.repo, config.load_at(self.repo).cfg)
        cfg = self.cfg(command="npx tsx {test}", always=["npm run typecheck"])
        self.assertEqual(test_stage.commands(cfg, g, ["a.ts", "b.ts"]),
                         [["npm", "run", "typecheck"], ["npx", "tsx", "a.ts"], ["npx", "tsx", "b.ts"]])
        # The whole suite in the per-file form is every test file the graph knows.
        self.assertEqual(test_stage.commands(cfg, g, None)[1:], [["npx", "tsx", "tests/test_calc.py"]])
        self.assertEqual(test_stage.commands(self.cfg(command="run {tests}"), g, None), [["run", "tests/test_calc.py"]])

    def test_select_tests_takes_a_helpers_importers_and_the_whole_suite_for_proof_grade(self):
        # Guards c446: a file under the [graph] tests globs that tests import is a helper, so touching it selects the
        # tests that import it (running it alone finds no tests, exit 5); a real test file still selects itself.
        (self.repo / "tests" / "helpers.py").write_text("# id: tests.helpers\n# what: x\n# cmd: -\n# reads: -\n# writes: -\nX = 1\n")
        t = self.repo / "tests" / "test_calc.py"
        t.write_text(t.read_text().replace("import unittest\n", "import unittest\n\nfrom tests.helpers import X  # noqa: F401\n", 1))
        self.git("add", "-A")
        cfg = config.load_at(self.repo).cfg
        g = graph.build(self.repo, cfg)
        self.assertEqual(graph.select_tests(g, ["tests/helpers.py"], cfg), ["tests/test_calc.py"])
        self.assertEqual(graph.select_tests(g, ["tests/test_calc.py"], cfg), ["tests/test_calc.py"])
        # Guards the strict level (CHARTER section 4): a proof-grade path never gets the graph's narrower selection,
        # even when the graph knows exactly which tests reach it.
        self.assertTrue(graph.select_tests(g, ["app/calc.py"], cfg))
        cfg["rigour"] = {**cfg["rigour"], "proof_grade": ["app/calc.py"]}
        self.assertIsNone(graph.select_tests(g, ["app/calc.py"], cfg))


class ReadsProseTest(FixtureCase):
    # Guards the contract: a test whose header reads a folder is selected for any file under it (prose included), and not for files elsewhere.
    # Guards c49 too: a Python test's string literal that is a tracked path (a subprocess argv, a fixture folder) reaches
    # it, as a header naming it does; a string that is no tracked path reaches nothing.
    def test_header_folder_and_string_literal_path_reach_what_they_name(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "a.md").write_text("a\n")
        (self.repo / "other.md").write_text("b\n")
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "tool").write_text("echo\n")
        (self.repo / "fix").mkdir()
        (self.repo / "fix" / "data.txt").write_text("d\n")
        (self.repo / "tests").mkdir(exist_ok=True)
        (self.repo / "tests" / "test_docs.py").write_text(
            "# id: tests.test_docs\n# what: x\n# cmd: python3 -m unittest tests/test_docs.py\n# reads: docs\n# writes: -\n")
        (self.repo / "tests" / "test_cli.py").write_text(
            "# id: tests.test_cli\n# what: x\n# cmd: -\n# reads: -\n# writes: -\n"
            "ARGV = ['bin/tool', '--help']\nFIX = 'fix/'\nNOTE = 'other'\n")
        self.git("add", "-A")
        cfg = config.load_at(self.repo).cfg
        g = graph.build(self.repo, cfg)
        self.assertIn("tests/test_docs.py", g["tests"]["docs/a.md"])
        self.assertNotIn("other.md", g["tests"])
        self.assertIn("tests/test_docs.py", graph.select_tests(g, ["docs/a.md"], cfg))
        self.assertIn("tests/test_cli.py", g["tests"]["bin/tool"])
        self.assertIn("tests/test_cli.py", g["tests"]["fix/data.txt"])

    # Guards c169: prose under a [graph] history glob makes no doc edge, so no brief tells an
    # agent to update it; the same prose outside the glob does.
    def test_history_docs_make_no_doc_edge(self):
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "tool.py").write_text("# id: bin.tool\n# what: x\n# cmd: -\n# reads: -\n# writes: -\n")
        (self.repo / "log").mkdir()
        (self.repo / "log" / "past.md").write_text("bin/tool.py was added\n")
        (self.repo / "now.md").write_text("bin/tool.py is used, ./bin/tool.py and bin/tool.py.\n")
        self.git("add", "-A")
        cfg = config.load_at(self.repo).cfg
        cfg["graph"]["history"] = ["log/*"]
        g = graph.build(self.repo, cfg)
        self.assertEqual(g["docs"]["bin/tool.py"], ["now.md"])
        # Also guards c357: files a sandbox will not let us read keep their committed entries and are listed.
        (self.repo / cfg["graph"]["out"]).write_text(graph.render(g))
        real = host.read

        def denied(p, errors="strict"):
            if str(p).endswith(("now.md", "tool.py")):
                raise PermissionError(p)
            return real(p, errors)
        with mock.patch.object(host, "read", denied):
            again = graph.build(self.repo, cfg)
        self.assertEqual(graph.render(again), graph.render(g))
        self.assertEqual(again["unreadable"], ["bin/tool.py", "now.md"])
        # Also guards c509: a tracked file whose stat is denied is kept the same way; a tracked file deleted
        # from the worktree (still listed by git ls-files) is dropped, not carried from the old graph.
        real_stat = pathlib.Path.stat

        def stat_denied(s, *a, **k):
            if str(s).endswith(("now.md", "tool.py")):
                raise PermissionError(s)
            return real_stat(s, *a, **k)
        (self.repo / "log" / "past.md").unlink()
        with mock.patch.object(pathlib.Path, "stat", stat_denied):
            again = graph.build(self.repo, cfg)
        self.assertEqual(again["unreadable"], ["bin/tool.py", "now.md"])
        self.assertEqual(graph.render(again), graph.render(g))
