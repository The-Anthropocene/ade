# id: tests.test_recurring
# what: a recurring card rests after it lands and opens again on its due date; a snooze moves that date, and the next landing resets it
# cmd: python3 -m unittest tests/test_recurring.py
# reads: tests/fixture_repo
# writes: temporary directories only
from datetime import date, timedelta

from ade import config, records
from tests.helpers import FixtureCase


class RecurringTest(FixtureCase):
    def setUp(self):
        super().setUp()
        self.install()
        code, out = self.ade("card", "add", "re-check the published burn numbers against the live meter", "--path", "app/calc.py", "--done", "the meter table is dated this week",
                             "--every", "7")
        self.assertEqual(code, 0, out)
        self.ctx = config.load(self.repo)

    def status(self, days=0):
        return records.merged(self.ctx, "card", date.today() + timedelta(days=days))["c1"]["status"]

    def land(self):
        records.append(self.ctx, "card", {"id": "c1", "status": "landed", "commit": "abc"})

    # Guards the contract: a recurring card is open until it lands, rests for `every` days, then is open by itself.
    def test_a_landed_recurring_card_rests_then_opens_on_its_due_date(self):
        self.assertEqual(self.status(), "open")
        self.land()
        self.assertEqual(self.status(), "landed")
        self.assertEqual(self.status(6), "landed")
        self.assertEqual(self.status(7), "open")

    # Guards the contract: a snooze only delays the card, and a later landing counts from itself, not from the snooze.
    def test_a_snooze_delays_it_the_next_landing_resets_the_date_and_a_one_off_cannot_be_snoozed(self):
        code, out = self.ade("card", "snooze", "c1", "--until", "3d")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.status(2), "landed")
        self.assertEqual(self.status(3), "open")
        self.ade("card", "snooze", "c1", "--until", "30d")
        self.land()
        self.assertEqual(self.status(7), "open")
        # Guards the contract: only a recurring card rests; a one-off card cannot be snoozed out of sight.
        self.ade("card", "add", "one off", "--path", "app/calc.py", "--done", "done")
        code, out = self.ade("card", "snooze", "c2", "--until", "3d")
        self.assertNotEqual(code, 0)
        self.assertIn("card.snooze", out)
        self.assertEqual(records.merged(self.ctx, "card")["c2"]["status"], "open")
