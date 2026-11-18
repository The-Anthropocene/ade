# id: tests.test_records
# what: durable rows reach the ade-records branch without moving the checkout, and come back if .ade/ is lost
# cmd: python -m unittest tests.test_records
# reads: ade/records.py
# writes: temporary directories only
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from ade import config, records
from ade.gitx import git
from ade.refuse import Refusal
from tests.helpers import FixtureCase


class RecordsBranch(FixtureCase):
    def setUp(self):
        super().setUp()
        # Run from inside a lane, ADE_ROLE is set and no backup happens: clear it so these tests test the branch.
        env = mock.patch.dict("os.environ")
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ADE_ROLE", None)

    def branch_rows(self):
        # Through ade's own git helper: it decodes UTF-8 on every platform, where text mode would use the locale.
        return [json.loads(line) for line in git(self.repo, "show", "ade-records:records.jsonl").splitlines() if line]

    def test_durable_rows_land_on_the_branch_and_the_checkout_stays_put(self):
        # Guards the branch contract: durable rows reach ade-records by plumbing alone; cache and usage rows stay local.
        self.install()
        head, start = self.git("rev-parse", "HEAD"), self.commits()
        code, out = self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "noted")
        self.assertEqual(code, 0, out)
        code, out = self.ade("decide", "--path", "app/*", "calc stays pure: no I/O, ever — naïve or not")
        self.assertEqual(code, 0, out)
        records.append(config.load(str(self.repo)), "journal", {"text": "a cache row"})
        self.assertEqual([(r["type"], r["id"]) for r in self.branch_rows()], [("card", "c1"), ("decision", "d1")])
        self.assertEqual(self.branch_rows()[1]["text"], "calc stays pure: no I/O, ever — naïve or not")
        # Plumbing only: main, the index and the working tree never moved.
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.commits(), start + 2)  # one commit per durable row; the cache row adds none
        self.assertEqual(self.git("ls-tree", "--name-only", "ade-records").split(), ["records.jsonl"])
        # Also guards the privacy contract: no usage numbers leave the machine; only the share flag may.
        ctx = config.load(str(self.repo))
        records.append(ctx, "usage", {"card": "c1", "provider": "p", "tokens": 1})
        records.append(ctx, "share", {"on": True})
        self.assertEqual([r["type"] for r in self.branch_rows()], ["card", "decision", "share"])
        self.assertEqual(len(records.read(ctx, "usage")), 1)

    def test_a_backup_append_on_an_existing_branch_spawns_at_most_two_git_processes(self):
        # Guards the cost contract: the tip and its file come from one cat-file --batch, then one fast-import.
        self.install()
        ctx = config.load(str(self.repo))
        records.append(ctx, "observation", {"text": "first"})
        with mock.patch("subprocess.run", wraps=subprocess.run) as run:
            records.append(ctx, "observation", {"text": "second"})
        spawned = [c.args[0][1] for c in run.call_args_list if c.args and c.args[0][:1] == ["git"]]
        self.assertLessEqual(len(spawned), 2, spawned)

    def test_a_lane_appends_locally_and_the_next_plain_append_backs_it_up(self):
        # Guards the contract: inside a lane (ADE_ROLE set) no git ref is written, and no durable row is lost for it.
        self.install()
        ctx = config.load(str(self.repo))
        with mock.patch.dict("os.environ", {"ADE_ROLE": "builder"}):
            records.append(ctx, "decision", {"text": "from a lane"}, prefix="d")
        self.assertEqual(len(records.read(ctx, "decision")), 1)
        self.assertEqual(self.branch_rows(), [])
        records.append(ctx, "decision", {"text": "plain"}, prefix="d")
        self.assertEqual([r["text"] for r in self.branch_rows()], ["from a lane", "plain"])

    def test_a_lost_state_folder_comes_back_from_the_branch(self):
        self.install()
        self.ade("card", "add", "note the calc contract", "--path", "app/calc.py", "--done", "noted")
        (self.repo / ".ade" / "records.jsonl").unlink(missing_ok=True)
        code, out = self.ade("card", "list")
        self.assertEqual(code, 0, out)
        self.assertIn("note the calc contract", out)
        # The next id continues from the restored rows instead of minting c1 again.
        code, out = self.ade("card", "add", "second", "--path", "app/calc.py", "--done", "noted")
        self.assertEqual(code, 0, out)
        self.assertEqual([r["id"] for r in self.branch_rows()], ["c1", "c2"])

    def test_card_paths_replaces_only_the_paths_of_an_open_card(self):
        # Guards: `card paths` appends a card row that changes paths alone, warns on an unmatched path,
        # and refuses a closed card.
        self.install()
        self.ade("card", "add", "t", "--path", "app/calc.py", "--done", "d")
        code, out = self.ade("card", "paths", "c1", "--path", "app/calc.py", "--path", "nowhere/x.py")
        self.assertEqual(code, 0, out)
        self.assertIn("nowhere/x.py matches no tracked file", out)
        c = records.merged(config.load(str(self.repo)), "card")["c1"]
        self.assertEqual((c["paths"], c["title"], c["done"], c["status"]), (["app/calc.py", "nowhere/x.py"], "t", "d", "open"))
        self.assertIn("nowhere/x.py", self.ade("card", "show", "c1")[1])
        self.ade("card", "close", "c1", "--why", "w")
        code, out = self.ade("card", "paths", "c1", "--path", "app/calc.py")
        self.assertNotEqual(code, 0)
        self.assertIn("closed", out)

    def test_rows_from_before_the_branch_are_seeded_and_ids_never_repeat(self):
        self.install()
        local = self.repo / ".ade" / "records.jsonl"
        local.parent.mkdir(exist_ok=True)
        # A repo that kept records before this card: rows on disk, no branch yet.
        old = [{"type": "card", "id": "c1", "title": "old", "at": "2026-10-01T00:00:00+00:00"},
               {"type": "journal", "text": "a cache row", "at": "2026-10-01T00:00:00+00:00"}]
        local.write_text("".join(json.dumps(r) + "\n" for r in old))
        self.ade("card", "add", "new", "--path", "app/calc.py", "--done", "noted")
        self.assertEqual([(r["id"], r["title"]) for r in self.branch_rows()], [("c1", "old"), ("c2", "new")])
        local.unlink()
        self.ade("card", "add", "newer", "--path", "app/calc.py", "--done", "noted")
        cards = records.merged(config.load(str(self.repo)), "card")
        self.assertEqual({k: v["title"] for k, v in cards.items()}, {"c1": "old", "c2": "new", "c3": "newer"})

    def commits(self):
        return int(self.git("rev-list", "--count", "ade-records"))

    def test_two_appends_leave_a_linear_branch_whose_tip_equals_the_local_rows(self):
        # Guards the branch's shape (one parent per commit, tip file = local durable rows) however the commit is written.
        self.install()
        start = self.commits()  # init may already have written a start commit
        for text in ("one", "two naïve"):
            code, out = self.ade("decide", "--path", "app/*", text)
            self.assertEqual(code, 0, out)
        ctx = config.load(str(self.repo))
        self.assertEqual(self.commits(), start + 2)
        self.assertEqual(self.git("rev-list", "--merges", "ade-records").strip(), "")
        with records._path(ctx).open(encoding="utf-8") as f:
            local = [r for r in records._rows(f) if r["type"] in records.DURABLE]
        self.assertEqual(self.branch_rows(), local)

    def test_init_seeds_the_branch_and_says_to_push_it(self):
        # Records on disk that the branch lacks (written before card 8, or by a kit that never seeded): init adds them.
        self.install()
        local = self.repo / ".ade" / "records.jsonl"
        local.parent.mkdir(exist_ok=True)
        local.write_text(json.dumps({"type": "decision", "id": "d1", "text": "old", "paths": ["*"], "at": "x"}) + "\n")
        self.git("remote", "add", "origin", str(self.tmp / "nowhere.git"))
        code, out = self.ade("init")
        self.assertEqual(code, 0, out)
        self.assertEqual([r["id"] for r in self.branch_rows()], ["d1"])
        self.assertIn("is only on this machine: ade push", out)
        seeded = self.commits()
        self.assertEqual(self.ade("init")[0], 0)
        self.assertEqual(self.commits(), seeded)  # a rerun adds nothing
        # Also guards init with no records on disk and no branch (a second machine after pulling card 13): it starts the branch.
        local.unlink()
        self.git("branch", "-D", "ade-records")
        code, out = self.ade("init")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.branch_rows(), [])
        self.assertIn("is only on this machine: ade push", out)
        # The first record lands on top of the empty start, and a lost .ade/ still restores it.
        self.assertEqual(self.ade("card", "add", "first", "--path", "app/calc.py", "--done", "noted")[0], 0)
        self.assertEqual([r["id"] for r in self.branch_rows()], ["c1"])
        first = self.commits()
        self.assertEqual(self.ade("init")[0], 0)
        self.assertEqual(self.commits(), first)  # a rerun adds nothing

    def test_a_second_clone_continues_the_pushed_branch(self):
        # A second machine: the branch was pushed from the first, then the repo was cloned fresh.
        self.install()
        self.ade("card", "add", "from machine A", "--path", "app/calc.py", "--done", "noted")
        bare = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", str(self.repo), str(bare))
        clone = self.tmp / "b"
        self.git("clone", "-q", str(bare), str(clone))
        tip = git(clone, "rev-parse", "origin/ade-records")
        self.repo = clone  # self.ade and self.git now run in the clone
        code, out = self.ade("init")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git("rev-parse", "ade-records").strip(), tip)  # adopted, not started anew
        code, out = self.ade("card", "list")
        self.assertIn("from machine A", out)
        self.ade("card", "add", "from machine B", "--path", "app/calc.py", "--done", "noted")
        self.assertEqual([r["id"] for r in self.branch_rows()], ["c1", "c2"])
        self.assertEqual(self.git("merge-base", "--is-ancestor", tip, "ade-records").strip(), "")  # a fast-forward
        self.git("push", "-q", "origin", "ade-records")

    def test_a_new_id_counts_what_another_machine_pushed(self):
        # Guards: an id is never minted that the remote already holds, even when this clone has not fetched it yet.
        self.install()
        mine = self.repo
        bare = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", str(mine), str(bare))
        self.git("remote", "add", "origin", str(bare))
        other = self.tmp / "other"
        self.git("clone", "-q", str(bare), str(other))
        self.repo = other
        self.assertEqual(self.ade("init")[0], 0)
        records.append(config.load(str(other)), "card", {"id": "c7", "title": "elsewhere"})
        self.git("push", "-q", "origin", "ade-records")
        self.repo = mine
        self.assertEqual(self.ade("card", "add", "mine", "--path", "app/calc.py", "--done", "noted")[0], 0)
        self.assertEqual(self.branch_rows()[-1]["id"], "c8")

    def test_an_id_renumbered_away_refuses_unless_theirs_is_asked_for(self):
        # Guards: after reconcile moves our card off an id the other machine minted, that id never silently resolves
        # to the other machine's card: it refuses naming both titles, and --theirs (theirs=True) is the way through.
        from ade import stages
        from ade.refuse import Refusal
        self.install()
        mine = self.repo
        bare = self.tmp / "origin.git"
        self.git("clone", "-q", "--bare", str(mine), str(bare))
        self.git("remote", "add", "origin", str(bare))
        other = self.tmp / "other"
        self.git("clone", "-q", str(bare), str(other))
        self.repo = other
        self.assertEqual(self.ade("init")[0], 0)
        # Both machines write the same id between pushes (minting now counts the remote, so the ids are given).
        row = {"status": "open", "paths": ["app/calc.py"], "done": "noted"}
        records.append(config.load(str(other)), "card", {**row, "id": "c7", "title": "theirs card"})
        self.git("push", "-q", "origin", "ade-records")
        self.repo = mine
        ctx = config.load(str(mine))
        records.append(ctx, "card", {**row, "id": "c7", "title": "mine card"})
        self.git("fetch", "-q", "origin", "ade-records")
        moved = records.reconcile(ctx, self.git("rev-parse", "FETCH_HEAD").strip())
        self.assertEqual(len(moved), 1)
        old, new = next(iter(moved.items()))
        with self.assertRaises(Refusal) as cm:
            stages.card(ctx, old)
        self.assertEqual(cm.exception.code, "card.renumbered")
        self.assertIn("mine card", cm.exception.cause)
        self.assertIn("theirs card", cm.exception.cause)
        self.assertIn(f"ade loop {new}", cm.exception.cure)
        self.assertEqual(stages.card(ctx, old, theirs=True)["title"], "theirs card")
        self.assertEqual(stages.card(ctx, new)["title"], "mine card")
        # Naming a renumbered id to read or close it runs nothing: show says it moved, close lands on the named card.
        code, out = self.ade("card", "show", old)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{old} was renumbered: yours is now {new}", out)
        self.assertIn("theirs card", out)
        code, out = self.ade("card", "close", old, "--why", "x")
        self.assertEqual(code, 0, out)
        self.assertEqual(records.merged(ctx, "card")[old]["status"], "closed")
        self.assertEqual(records.merged(ctx, "card")[new]["status"], "open")

    def test_fired_never_raises_and_keeps_the_local_row_when_the_backup_fails(self):
        # Guards the contract: refusal rows reach the branch and restore, carrying only type, at, code (and card).
        self.install()
        ctx = config.load(str(self.repo))
        records.fired(ctx, mock.Mock(code="x.y"))
        (self.repo / ".ade" / "records.jsonl").unlink(missing_ok=True)
        got = records.read(ctx, "refusal")
        self.assertEqual([r["code"] for r in got], ["x.y"])
        self.assertEqual(set(got[0]), {"type", "at", "code"})
        # Also guards the contract: a refusal row that cannot be backed up (git refusing a non-fast-forward) must not
        # turn a refusal into a traceback, and the local row stays for the next durable append to back up.
        with mock.patch.object(records, "_backup", side_effect=RuntimeError("non-fast-forward")):
            records.fired(ctx, Refusal("guard.secret", "no", "none"))
        self.assertEqual([r["code"] for r in records.read(ctx, "refusal")], ["x.y", "guard.secret"])
        # Also guards: a refusal fired before .ade/ exists is written, not silently dropped (the fallback makes the dir).
        shutil.rmtree(self.repo / ".ade")
        with mock.patch.object(records, "append", side_effect=RuntimeError("no dir")):
            records.fired(ctx, Refusal("guard.secret", "no", "none"))
        self.assertEqual([r["code"] for r in records.read(ctx, "refusal")], ["guard.secret"])


if __name__ == "__main__":
    unittest.main()
