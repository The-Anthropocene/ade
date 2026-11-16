# id: tests.test_q
# what: the output store's invariant: output over the cap never reaches the screen whole, and comes back byte for byte
# cmd: python3 -m unittest tests/test_q.py
# reads: tests/fixture_repo
# writes: temporary directories only
import re
import sys

from ade import q  # noqa: F401  (imported so the graph records coverage)
from ade.verbs import q as qverb  # noqa: F401
from tests.helpers import FixtureCase


class StoreTest(FixtureCase):
    def test_long_output_is_stored_whole_and_shown_short(self):
        self.install()
        script = "for i in range(5000): print(f'line {i}')\n"
        (self.tmp / "long.py").write_text(script)
        code, out = self.ade("q", "run", "--", sys.executable, str(self.tmp / "long.py"))
        self.assertEqual(code, 0, out)
        self.assertLess(len(out.splitlines()), 200)  # the screen gets head, tail and a handle, never the whole
        qid = re.search(r"stored as (q\d+)", out)[1]
        whole = []
        for page in range(1, 100):
            _, text = self.ade("q", "show", qid, "--page", str(page))
            body, footer = text.rstrip("\n").rsplit("\n", 1)
            whole += body.splitlines()
            if re.search(r"page (\d+) of \1\]", footer):
                break
        self.assertEqual(whole, [f"line {i}" for i in range(5000)])  # every line, in order, nothing lost

        short_code, short = self.ade("q", "run", "--", sys.executable, "-c", "print('hi'); raise SystemExit(3)")
        # Short output passes through untouched, exit code too (a Windows child ends its lines with \r\n).
        self.assertEqual((short_code, short.splitlines()), (3, ["hi"]))

        code, rows = self.ade("q", "sql", "SELECT kind, count(*) FROM edges GROUP BY kind")
        self.assertEqual(code, 0, rows)
        self.assertIn("imports", rows)
        self.assertEqual(self.ade("q", "sql", "DELETE FROM outputs")[0], 2)  # read-only: a write is refused
