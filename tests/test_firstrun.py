# id: tests.test_firstrun
# what: the welcome appears only while the machine defaults file is missing, and never replaces the verb list
# cmd: python3 -m unittest tests/test_firstrun.py
# reads: ade/firstrun.py, ade/home.py
# writes: temporary directories only
import os
import tempfile
from unittest import mock

from ade import home
from tests.helpers import FixtureCase

LAST_VERB = "ade " + home.GROUPS[-1][1][-1][0]  # the verb list's last row, printed nowhere else


class FirstRunTest(FixtureCase):
    def test_welcome_shows_until_defaults_exist_which_seed_the_first_ade_toml_and_a_broken_file_refuses(self):
        # Guards the contract: a broken machine defaults file refuses with a cure.
        os.makedirs(os.environ["XDG_CONFIG_HOME"] + "/ade")
        open(os.environ["XDG_CONFIG_HOME"] + "/ade/defaults.toml", "w").write("[parts\n")
        folder = self.tmp / "broken"
        folder.mkdir()
        code, out = self.ade("init", cwd=folder)
        self.assertNotEqual(code, 0)
        self.assertIn("defaults.broken", out)
        self.assertIn("cure:", out)
        # Guards the contract: the welcome goes above the verbs, and stops once the defaults file exists.
        with tempfile.TemporaryDirectory() as cfg, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg, "APPDATA": cfg}), \
                mock.patch("sys.platform", "linux"):
            code, out = self.ade()
            self.assertEqual(code, 0, out)
            self.assertLess(out.index("Welcome"), out.index(LAST_VERB))
            self.assertIn("ade init", out)
            os.makedirs(os.path.join(cfg, "ade"))
            open(os.path.join(cfg, "ade", "defaults.toml"), "w").close()
            code, out = self.ade()
            self.assertNotIn("Welcome", out)
            self.assertIn(LAST_VERB, out)
        # Guards the contract: `ade setup` outside a project saves the machine defaults, and the first ade.toml
        # `ade init` writes starts from their [parts] while the starter's other settings are kept.
        from ade import parts
        orca = list(parts.catalogue()).index("orca") + 1
        with tempfile.TemporaryDirectory() as cfg, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg, "APPDATA": cfg}), \
                mock.patch("sys.platform", "linux"):
            outside = self.tmp / "outside"
            outside.mkdir()
            code, out = self.ade("setup", stdin=f"{orca}\n\ny\n", cwd=outside)
            self.assertEqual(code, 0, out)
            self.assertTrue(os.path.exists(os.path.join(cfg, "ade", "defaults.toml")), out)
            folder = self.tmp / "fresh"
            folder.mkdir()
            code, out = self.ade("init", "--starter", "typescript", cwd=folder)
            self.assertEqual(code, 0, out)
            toml = (folder / "ade.toml").read_text()
            self.assertIn('off = ["orca"]', toml)
            self.assertIn("node --experimental-strip-types", toml)
