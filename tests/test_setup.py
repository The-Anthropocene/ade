# id: tests.test_setup
# what: the setup panel's model agrees with `ade parts set`: switching a part in the panel gives the same ade.toml text, and its cursor never leaves the list
# cmd: python3 -m unittest tests/test_setup.py
# reads: ade/parts.toml
# writes: temporary directories only
from unittest import mock

from ade import firstrun, host, install, parts
from ade.setup import Panel
from tests.helpers import FixtureCase


class PanelTest(FixtureCase):
    # Guards the P3 contract: for every part, toggling it off in the panel writes what `ade parts set x=off` writes.
    # Also guards: the panel's cursor never leaves the list.
    def test_toggle_matches_parts_set_and_the_cursor_stays_in_the_list(self):
        self.install()
        f = self.repo / "ade.toml"
        base = f.read_text(encoding="utf-8")
        cat = parts.catalogue()
        for pid in cat:
            panel = Panel(cat, set(), {})
            panel.toggle(pid)
            self.assertEqual(self.ade("parts", "set", f"{pid}=off")[0], 0)
            self.assertEqual(panel.new_text(base), f.read_text(encoding="utf-8"), pid)
            f.write_text(base, encoding="utf-8")
        panel = Panel(parts.catalogue(), set(), {})
        for step in (-1, -1, 1, 7, -9):
            panel.move(step)
            panel.cycle()
            self.assertIn(panel.ids[panel.at], panel.cat)
        # Guards the root rule: a first ade.toml gets a lane_mode Claude Code will start (auto as root).
        for uid, want in ((0, "auto"), (1000, "bypassPermissions")):
            with mock.patch("os.geteuid", return_value=uid, create=True), mock.patch("builtins.print"):
                self.assertEqual(want == "auto", 'lane_mode = "auto"' in firstrun.with_defaults(host.read(install.TEMPLATE)))
