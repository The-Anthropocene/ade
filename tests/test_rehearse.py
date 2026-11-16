# id: tests.test_rehearse
# what: the rehearsal runner lands a card per scenario and fails, naming the refusal, when a scenario does not land
# cmd: python3 -m unittest tests/test_rehearse.py
# reads: the kit
# writes: temporary directories only
from unittest import mock

from ade.verbs import rehearse
from tests.helpers import FixtureCase


class RehearseTest(FixtureCase):
    def test_a_scenario_that_does_not_land_fails_with_its_refusal_and_coverage_lists_unflown_verbs(self):
        broken = {"steps": [["init"], "commit", ["loop", "c9"]]}
        with mock.patch.dict(rehearse.SCENARIOS, {"broken": broken}):
            code, out = self.ade("rehearse", "--only", "broken")
        self.assertEqual(code, 1, out)
        self.assertIn("fail  broken", out)
        self.assertIn("card.unknown", out)
        # Guards that --coverage derives its list from the scenarios' steps: with none, every verb but the runner and init is listed.
        from ade import cli
        with mock.patch.dict(rehearse.SCENARIOS, {}, clear=True):
            code, out = self.ade("rehearse", "--coverage")
        self.assertEqual(code, 0, out)
        listed = out.splitlines()[0].split(": ")[1].split(", ")
        self.assertEqual(set(listed), set(cli.verbs()) - {"rehearse", "init"})
