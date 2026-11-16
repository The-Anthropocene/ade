# id: tests.test_draft
# what: a draft's prompt carries the test command, PROOF lines are stored as the card's proofs, a path outside the graph is refused, and an afternoon draft's veto deadline counts from tomorrow
# cmd: python3 -m unittest tests/test_draft.py
# reads: tests/fixture_repo
# writes: temporary directories only
from datetime import date, datetime, timedelta
from unittest import mock

from ade import config, providers, records
from ade.verbs import card
from tests.helpers import FixtureCase

GOOD = "TITLE: note the calc contract\nPATH: app/calc.py\nDONE: a comment names the contract\n"


class DraftTest(FixtureCase):
    def draft(self, reply):
        self.install()
        self.ade("graph")
        with mock.patch.object(providers, "call", return_value=reply):
            return self.ade("card", "draft", "document calc")

    # The accept/hold contract is flown by `ade rehearse --only draft`, so no unit test repeats it.

    def test_a_path_outside_the_graph_is_refused_and_the_prompt_and_proofs_of_a_good_draft(self):
        # Guards the contract: a path the graph and the folders do not know writes no card.
        code, out = self.draft(GOOD.replace("app/calc.py", "nowhere/x.py"))
        self.assertNotEqual(code, 0)
        self.assertIn("card.draft", out)
        self.assertFalse(records.merged(config.load(self.repo), "card"))
        for outside in ("../app/x.py", "/tmp/x.py"):
            code, out = self.draft(GOOD.replace("app/calc.py", outside))
            self.assertIn("card.draft", out, outside)
        self.assertFalse(records.merged(config.load(self.repo), "card"))
        # Guards the contract: PROOF lines in the pilot's reply are stored as the card's proofs list and printed.
        cmd = "{python} -m unittest tests/test_calc.py"
        with mock.patch.object(providers, "call", return_value=GOOD + f"PROOF: {cmd}\n") as call:
            code, out = self.ade("card", "draft", "document calc")
        self.assertEqual(code, 0, out)
        self.assertEqual(records.merged(config.load(self.repo), "card")["c1"]["proofs"], [cmd])
        self.assertIn(f"proof: {cmd}", out)
        # Guards the contract: the pilot's prompt names the configured test command, so a DONE line cites checks the project can run.
        self.assertIn(config.load(self.repo).cfg["tests"]["command"], call.call_args[0][2])
        # Guards the next-morning rule: a draft at or after midday counts its accept days from tomorrow, one before
        # midday from today, and the draft prints the deadline it set.
        days = int(config.load(self.repo).cfg.get("cards", {}).get("accept", 0))
        for hour, base in ((11, date(2026, 11, 4)), (12, date(2026, 11, 5)), (23, date(2026, 11, 5))):
            with mock.patch.object(providers, "call", return_value=GOOD), \
                    mock.patch.object(card, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 11, 4, hour, 30)
                code, out = self.ade("card", "draft", "document calc")
            self.assertEqual(code, 0, out)
            deadline = (base + timedelta(days=days)).isoformat()
            asks = [d for d in records.merged(config.load(self.repo), "decision").values() if d.get("rec") == "accept"]
            self.assertEqual(max(asks, key=lambda d: int(d["id"][1:]))["deadline"], deadline, hour)
            self.assertIn(deadline, out)
