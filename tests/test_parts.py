# id: tests.test_parts
# what: the parts catalogue stays true to the template: every key a part owns is in ade/template.toml, every need and leans-on names a known part
# cmd: python3 -m unittest tests/test_parts.py
# reads: ade/parts.toml, ade/template.toml
# writes: -
import tomllib
import unittest

from ade import config, parts


class PartsCatalogue(unittest.TestCase):
    # Guards the catalogue's contract: a renamed key or part cannot leave `ade parts` pointing at nothing.
    def test_keys_and_links_resolve(self):
        template = tomllib.loads(config.TEMPLATE.read_text(encoding="utf-8"))
        cat = parts.catalogue()
        for pid, c in cat.items():
            for key in c["keys"]:
                node = template
                for part in key.split("."):
                    self.assertIn(part, node, f"{pid}: {key} is not in template.toml")
                    node = node[part]
            for other in c["needs"] + c["leans-on"]:
                self.assertIn(other, cat, f"{pid} names unknown part {other}")


if __name__ == "__main__":
    unittest.main()
