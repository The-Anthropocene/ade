# id: tests.test_link
# what: ade link puts one launcher named ade in a per-user folder already on PATH, never the venv's bin, never over a stranger's file
# cmd: python3 -m unittest tests/test_link.py
# reads: -
# writes: temporary directories only
import os
import shutil
from unittest import mock

from ade.verbs import link  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase


class LinkTest(FixtureCase):
    def test_one_launcher_in_a_user_folder_on_path_and_nothing_when_there_is_none(self):
        home = self.tmp / "home"
        mine, venv = home / ".local" / "bin", self.tmp / "venv" / "bin"
        mine.mkdir(parents=True)
        venv.mkdir(parents=True)
        tools = os.path.dirname(shutil.which("git"))  # git stays reachable; nothing else from this machine's PATH
        env = {"HOME": str(home), "USERPROFILE": str(home), "PATH": os.pathsep.join([str(venv), tools])}
        with mock.patch.dict(os.environ, env):
            code, out = self.ade("link")  # no per-user folder on PATH: an alias to copy, and nothing written
            self.assertEqual(code, 0, out)
            self.assertIn("nothing was written", out)
            self.assertEqual(list(mine.iterdir()) + list(venv.iterdir()), [])
            os.environ["PATH"] = os.pathsep.join([str(venv), str(mine), tools])
            self.assertEqual(self.ade("link")[0], 0)
            [shim] = list(mine.iterdir())
            self.assertEqual(shim.stem, "ade")
            self.assertEqual(os.path.normcase(shutil.which("ade")), os.path.normcase(str(shim)))  # Windows says ade.CMD
            self.assertIn("ade", shim.read_text())
            self.assertEqual(self.ade("link")[0], 0)  # its own launcher is rewritten, not refused
            shim.write_text("someone else's ade\n")  # on PATH or not, a file ade link did not write stays as it was
            self.ade("link")
            shim.chmod(0o644)
            self.ade("link")
            self.assertEqual(shim.read_text(), "someone else's ade\n")
