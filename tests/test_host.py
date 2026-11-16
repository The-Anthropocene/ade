# id: tests.test_host
# what: the platform layer: LF writes, {python} substitution that survives Windows paths, and a lock that holds across processes
# cmd: python3 -m unittest tests/test_host.py
# reads: -
# writes: temporary directories only
import json
import shutil
import subprocess
import sys
import tempfile
import io
import types
import unittest
from unittest import mock
from pathlib import Path

from ade import cli, host, records  # noqa: F401  (imported so the graph records coverage)
from ade.config import KIT, Ctx

APPEND = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from ade import records
from ade.config import Ctx
for _ in range(25):
    records.append(Ctx(Path(sys.argv[2]), {}), "card", {"title": "t"}, prefix="c")
"""


HOLD = """
import sys, time
sys.path.insert(0, sys.argv[1])
from ade import host
with host.lock(sys.argv[2]):
    print("held", flush=True)
    time.sleep(3)
"""


class HostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ade-host-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_write_is_lf_and_utf8_and_unencodable_output_is_replaced_not_raised(self):
        p = self.tmp / "f.txt"
        host.write(p, "a\nb · c\n")
        self.assertEqual(p.read_bytes(), "a\nb · c\n".encode("utf-8"))
        self.assertEqual(host.read(p), "a\nb · c\n")
        # Guards: a console encoding that cannot hold a printed character (cp1252 and the band's check mark) never crashes ade.
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")  # newline fixed so Windows adds no \r
        verb = types.SimpleNamespace(run=lambda args, cwd: print("✓ ok"))
        fake = types.SimpleNamespace(parse_args=lambda argv: types.SimpleNamespace(verb="v"))
        with mock.patch.object(cli, "parser", return_value=(fake, dict(v=verb))), mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["v"], cwd=str(self.tmp)), 0)
        out.flush()
        self.assertEqual(raw.getvalue(), b"? ok\n")

    def test_substitute_keeps_paths_whole(self):
        py = r"C:\Program Files\Python311\python.exe"
        argv = host.substitute(["{python}", "-m", "unittest", "{tests}"], python=py, tests=["tests/a.py", "tests/b.py"])
        self.assertEqual(argv, [py, "-m", "unittest", "tests/a.py", "tests/b.py"])
        self.assertEqual(host.substitute(["x", "{tests}"], tests=[]), ["x"])

    def test_lock_holds_across_processes_and_readers_never_wait_on_it(self):
        procs = [subprocess.Popen([sys.executable, "-c", APPEND, str(KIT), str(self.tmp)]) for _ in range(4)]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0] * 4)
        lines = host.read(self.tmp / ".ade" / "records.jsonl").splitlines()
        ids = [json.loads(x)["id"] for x in lines]
        self.assertEqual(len(ids), 100)
        self.assertEqual(len(set(ids)), 100)  # every id minted under the lock is unique
        lock = self.tmp / ".ade" / "records.lock"
        holder = subprocess.Popen([sys.executable, "-c", HOLD, str(KIT), str(lock)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.assertEqual(len(records.read(Ctx(self.tmp, {}))), 100)  # a mandatory lock on the data file fails here
