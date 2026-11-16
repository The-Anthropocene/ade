# id: tests.test_budget
# what: per-component budgets: a component over its lines or a core file in no component fails, and land nets by component
# cmd: python3 -m unittest tests/test_budget.py
# reads: tests/fixture_repo
# writes: temporary directories only
import json

from ade import budget, config, records
from ade.refuse import Refusal
from ade.stages import test as test_stage
from ade.verbs import budget as budget_verb, loop  # noqa: F401
from tests.helpers import FixtureCase


class BudgetTest(FixtureCase):
    def components(self, text):
        # The template ships the budget off (core = []); these tests turn it on for the project's Python.
        toml = self.repo / "ade.toml"
        toml.write_text(toml.read_text().replace("core = []", 'core = ["*.py"]', 1) + text)
        self.git("commit", "-qam", "budget components")

    def test_a_component_over_or_unassigned_is_a_note_and_land_reports_net_lines_per_component(self):
        self.install()
        # Guards the contract: a user's project never sees the kit's own budgets (hook, refusal and test rows); the kit itself always does.
        names = lambda root: {r[0] for r in budget.measure(config.load(root))}
        self.assertFalse({"hooks", "refusal codes", "test functions"} & names(self.repo))
        self.assertTrue({"hooks", "refusal codes", "test functions"} <= names(config.KIT))
        toml = self.repo / "ade.toml"
        plain = toml.read_text()
        self.components('\n[budgets.components.app]\nglobs = ["app/calc.py"]\nlines = 1\n')
        code, out = self.ade("budget")
        # Guards the rule: component rows (over, unassigned) are notes and exit 0; only a hard row (core lines) fails.
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"app +\d+ / 1 +over \(note\)")
        self.assertRegex(out, r"unassigned +\d+ / 0 +over \(note\)")  # app/__init__.py belongs to no component
        self.assertEqual(budget.over([("core lines", 2, 1), ("app", 2, 1)]), ([("core lines", 2, 1)], [("app", 2, 1)]))
        toml.write_text(plain)
        self.components('\n[budgets.components.app]\nglobs = ["app/*"]\nlines = 1000\n')
        self.assertEqual(self.ade("budget")[0], 0)
        code, out = self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "comment added")
        self.assertEqual(code, 0, out)
        cid = out.split()[0]
        code, out = self.ade("loop", cid)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"merge summary: net lines by component: .*app \+\d+")
        rows = [json.loads(x) for x in (self.repo / ".ade/records.jsonl").read_text().splitlines()]
        outcome = [r for r in rows if r["type"] == "outcome"][-1]
        self.assertGreater(outcome["net"]["app"], 0)
        self.assertIn("app", outcome["budget"])

    def test_every_refusal_is_counted_against_its_code_and_whether_its_card_landed_after(self):
        # Retiring a code needs its firings counted; every code in the source is listed, never fired ones included.
        self.install()
        code, out = self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "noted")
        cid = out.split()[0]
        self.assertEqual(self.ade("loop", "c999")[0], 2)  # a refusal at the verb
        records.fired(config.load(str(self.repo)), Refusal("lane.dirty", "x", "y"), cid)  # one inside a stage
        self.assertEqual(self.ade("loop", cid)[0], 0)
        code, out = self.ade("budget", "--refusals")
        self.assertEqual(code, 0, out)
        for c in budget.refusal_codes():
            self.assertRegex(out, rf"(?m)^{c} +\d+ fired")
        self.assertRegex(out, r"(?m)^card.unknown +1 fired +0 on cards")
        self.assertRegex(out, r"(?m)^lane.dirty +1 fired +1 on cards")

    def test_rule_7_names_a_code_that_fired_on_two_cards_that_landed_anyway(self):
        # Guards charter rule 7: a code on two landed-anyway cards is named; one on a single card is not.
        self.install()
        ctx = config.load(str(self.repo))
        for _ in range(3):  # many firings on one landed card still count as one card
            records.fired(ctx, Refusal("lane.stale", "x", "y"), "c1")
        for card in ("c1", "c2"):
            records.fired(ctx, Refusal("lane.dirty", "x", "y"), card)
            records.append(ctx, "outcome", {"card": card})
        records.fired(ctx, Refusal("card.unknown", "x", "y"), "c1")
        code, out = self.ade("budget", "--refusals")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"(?m)^lane.stale +3 fired +3 on cards")  # landed after, so the old count would name it
        self.assertNotIn("rule 7: lane.stale", out)
        self.assertIn("rule 7: lane.dirty fired on 2 cards", out)
        self.assertNotIn("rule 7: card.unknown", out)

    def test_a_red_test_writes_a_row_and_two_untouched_reds_are_named(self):
        # Guards the two-strike contract: each red test gets a row (card, test, touched yes/no), and
        # `ade budget --tests` names a test red on two landed cards whose diffs never touched it.
        self.install()
        ctx = config.load(str(self.repo))
        out = "FAIL: test_x (tests.test_calc.CalcTest.test_x)\n"
        graph = {"files": {"tests/test_calc.py": {"imports": ["app/calc.py"]}}}
        for cid, paths in (("c1", ["docs/a.md"]), ("c2", ["docs/b.md"]), ("c3", ["app/calc.py"])):
            test_stage.note_reds(ctx, {"id": cid}, ["t"], out, graph, paths)
            if cid != "c3":
                records.append(ctx, "outcome", {"card": cid})
        rows = records.read(ctx, "test_red")
        self.assertEqual([(r["card"], r["touched"]) for r in rows], [("c1", False), ("c2", False), ("c3", True)])
        self.assertTrue(all(r["test"] == "tests.test_calc.CalcTest.test_x" for r in rows))
        code, text = self.ade("budget", "--tests")
        self.assertEqual(code, 0, text)
        self.assertIn("tests.test_calc.CalcTest.test_x: red on 2 landed cards", text)
