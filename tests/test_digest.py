# id: tests.test_digest
# what: ade digest names a landed and a parked card, and stays within ten lines
# cmd: python3 -m unittest tests/test_digest.py
# reads: tests/fixture_repo
# writes: temporary directories only
import os
from unittest import mock
from datetime import datetime, timedelta, timezone

from ade import config, records
from ade.verbs import digest  # noqa: F401  (imported so the graph records coverage)
from tests.helpers import FixtureCase


class DigestTest(FixtureCase):
    def test_a_landed_and_a_parked_card_are_both_named_in_under_ten_lines(self):
        # Guards the contract: the day's summary names every kind of event it covers, and stays short.
        self.install()
        ctx = config.load(str(self.repo))
        records.append(ctx, "card", {"id": "c1", "status": "landed"})
        records.append(ctx, "decision", {"text": "c2 parked: stuck", "paths": [], "kind": "question", "rec": "retry",
                                         "default": "retry", "deadline": records.today().isoformat(),
                                         "card": "c2"}, prefix="d")
        code, out = self.ade("digest")
        self.assertEqual(code, 0, out)
        self.assertIn("c1", out)
        self.assertIn("c2", out)
        self.assertLess(len(out.splitlines()), 10)
        self.assertNotIn("evidence:", out)
        # A revert commit after the landed record reads 'reverted'; a later landed record reads 'landed' again.
        # Anchored to noon UTC on one date, so no clock time can move a stamp into another day.
        day = datetime.now(timezone.utc).date().isoformat()
        now = datetime.fromisoformat(day + "T12:00:00+00:00")
        at = lambda s: (now - timedelta(seconds=s)).isoformat(timespec="seconds")
        records.append(ctx, "card", {"id": "c3", "status": "landed", "at": at(120)})
        when = {"GIT_COMMITTER_DATE": at(60), "GIT_AUTHOR_DATE": at(60)}
        with mock.patch.dict(os.environ, when):
            self.git("commit", "--allow-empty", "-m", 'Revert "land c3: x"')
        self.assertIn("reverted c3", self.ade("digest", "--day", day)[1])
        records.append(ctx, "card", {"id": "c3", "status": "landed", "at": at(0)})
        out = self.ade("digest", "--day", day)[1]
        self.assertIn("landed c3", out)
        self.assertNotIn("reverted c3", out)

    def test_evidence_line_appears_only_when_an_outcome_carries_proofs_or_pins(self):
        # Guards the contract: the digest counts the day's proofs and pins, and prints nothing extra without them.
        self.install()
        ctx = config.load(str(self.repo))
        records.append(ctx, "card", {"id": "c1", "status": "landed"})
        records.append(ctx, "outcome", {"card": "c1", "proofs": [{"cmd": "true", "rc": 0, "out_sha256": "0" * 64}],
                                        "pins": {"tests/test_x.py": 2}})
        code, out = self.ade("digest")
        self.assertEqual(code, 0, out)
        self.assertIn("evidence: 1 proofs run across 1 cards; 2 literal pins noted in 1 files", out)
