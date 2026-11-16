# id: tests.test_push
# what: ade push sends only trunk, ade-records and checkpoint tags, never forces, and merges records two machines wrote
# cmd: python -m unittest tests.test_push
# reads: ade/push.py, ade/records.py, ade/stages/land.py
# writes: temporary directories only
import contextlib
import io
import json
import os
import subprocess
from unittest import mock

from ade import config, records
from ade.gitx import git
from ade.stages import land
from tests.helpers import FixtureCase


class Push(FixtureCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.bare = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        self.git("remote", "add", "origin", str(self.bare))

    def remote(self, ref):
        r = subprocess.run(["git", "rev-parse", "-q", "--verify", ref], cwd=self.bare, capture_output=True, text=True)
        return r.stdout.strip() or None

    def test_only_trunk_records_and_checkpoint_tags_leave_the_machine(self):
        self.ade("card", "add", "a card", "--path", "app/calc.py", "--done", "noted")
        self.git("tag", "checkpoint/kit-v0")
        self.git("tag", "v-private")
        self.git("branch", "ade/c9")  # a lane branch
        self.assertEqual(self.ade("push", "--dry-run")[0], 0)
        self.assertIsNone(self.remote("refs/heads/main"))  # a dry run asks, never sends
        code, out = self.ade("push")
        self.assertEqual(code, 0, out)
        for ref in ("refs/heads/main", "refs/heads/ade-records", "refs/tags/checkpoint/kit-v0"):
            self.assertEqual(self.remote(ref), self.git("rev-parse", ref).strip(), ref)
        self.assertIsNone(self.remote("refs/tags/v-private"))
        self.assertIsNone(self.remote("refs/heads/ade/c9"))
        self.git("commit", "-q", "--allow-empty", "-m", "one more")
        code, out = self.ade("push", "--dry-run")
        self.assertNotIn("pushed", out)  # a dry run never says it sent, even for a ref the remote would take

    def test_refs_reads_every_local_branch_and_tag_in_one_git_spawn(self):
        # Guards the cost contract: naming the refs to push is one for-each-ref, however many branches and tags exist.
        from ade import push
        self.ade("card", "add", "a card", "--path", "app/calc.py", "--done", "noted")
        self.git("branch", "src")
        self.git("tag", "checkpoint/a")
        self.git("tag", "checkpoint/b")
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["mirror"] = ["src:dst"]
        calls = []
        real = push.git
        with mock.patch.object(push, "on_github", return_value=False), \
                mock.patch.object(push, "git", side_effect=lambda *a, **k: calls.append(a) or real(*a, **k)):
            out = push.refs(ctx)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("refs/heads/src:refs/heads/dst", out)

    def test_push_verb_under_pr_mode_sends_records_but_never_trunk(self):
        # Guards: with [git] push = "pr" the `ade push` verb never moves remote trunk (review stays the way in); records still go.
        from types import SimpleNamespace
        from ade.verbs import push as verb
        self.assertEqual(self.ade("push")[0], 0)
        trunk = self.remote("refs/heads/main")
        self.git("commit", "-q", "--allow-empty", "-m", "unreviewed")
        self.ade("card", "add", "a card", "--path", "app/calc.py", "--done", "noted")
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        with mock.patch.object(verb.config, "load", return_value=ctx):
            verb.run(SimpleNamespace(dry_run=False), str(self.repo))
        self.assertEqual(self.remote("refs/heads/main"), trunk)
        self.assertEqual(self.remote("refs/heads/ade-records"), self.git("rev-parse", "ade-records").strip())

    def test_a_diverged_trunk_is_refused_not_forced_and_records_from_both_machines_are_kept(self):
        self.assertEqual(self.ade("push")[0], 0)
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        self.ade("card", "add", "from B", "--path", "app/calc.py", "--done", "noted", cwd=other)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "B"], cwd=other, check=True)
        self.assertEqual(self.ade("push", cwd=other)[0], 0)
        theirs = self.remote("refs/heads/main")
        self.ade("card", "add", "from A", "--path", "app/calc.py", "--done", "noted")
        self.git("commit", "-q", "--allow-empty", "-m", "A")
        code, out = self.ade("push")
        self.assertEqual(code, 2, out)
        self.assertIn("push.rejected", out)
        self.assertEqual(self.remote("refs/heads/main"), theirs)  # never forced over
        rows = [json.loads(ln) for ln in git(self.bare, "show", "ade-records:records.jsonl").splitlines() if ln]
        self.assertEqual(sorted(r["title"] for r in rows if r["type"] == "card"), ["from A", "from B"])

    def test_an_id_both_machines_minted_stays_with_the_first_pusher_and_ours_moves_with_its_branch(self):
        # The invariant: after two machines' records meet, every minted id names one card, and a card already
        # pushed never changes its id.
        self.assertEqual(self.ade("push")[0], 0)
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        self.ade("card", "add", "from B", "--path", "app/calc.py", "--done", "noted", cwd=other)
        self.ade("card", "add", "from A", "--path", "app/calc.py", "--done", "noted")  # c1 here too: B not pushed yet
        self.assertEqual(self.ade("push", cwd=other)[0], 0)
        self.git("branch", "ade/c1")  # its lane branch
        code, out = self.ade("push")
        self.assertEqual(code, 0, out)
        self.assertIn("card c1 from this machine is now c2", out)
        for text in (git(self.bare, "show", "ade-records:records.jsonl"),
                     (self.repo / ".ade" / "records.jsonl").read_text()):
            cards = {r["id"]: r["title"] for r in map(json.loads, filter(None, text.splitlines())) if r["type"] == "card"}
            self.assertEqual(cards, {"c1": "from B", "c2": "from A"})
        self.assertEqual(self.git("branch", "--list", "ade/*").split(), ["ade/c2"])

    def test_a_landing_after_someone_elses_push_catches_up_by_merge_and_goes_up_unforced(self):
        # Guards: when the remote trunk moved, a landing merges it (both commits survive, nothing is forced) and
        # pushes; a conflicting move leaves local trunk exactly where it was.
        self.assertEqual(self.ade("push")[0], 0)
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        (other / "theirs.txt").write_text("B\n")
        (other / "shared.txt").write_text("B\n")
        subprocess.run(["git", "add", "-A"], cwd=other, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "B"], cwd=other, check=True)
        self.assertEqual(self.ade("push", cwd=other)[0], 0)
        theirs = self.remote("refs/heads/main")
        (self.repo / "ours.txt").write_text("A\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "A")
        ours = self.git("rev-parse", "HEAD").strip()
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "land"
        with mock.patch.dict(os.environ, {"ADE_PROVIDER": "real"}):
            (self.repo / "shared.txt").write_text("A\n")  # the same file both sides added: a conflict
            self.git("add", "shared.txt")
            self.git("commit", "-qm", "A2")
            mine = self.git("rev-parse", "HEAD").strip()
            with contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertIn("not pushed", land.ship(ctx))
            self.assertIn("conflict in: shared.txt", said.getvalue())  # a stop names the conflicting files
            self.assertEqual(self.git("rev-parse", "HEAD").strip(), mine)
            self.assertEqual(self.remote("refs/heads/main"), theirs)
            self.git("reset", "-q", "--hard", ours)
            self.assertIn("pushed to", land.ship(ctx))
        self.assertEqual(self.remote("refs/heads/main"), self.git("rev-parse", "HEAD").strip())
        for sha in (theirs, ours):
            merged = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "refs/heads/main"], cwd=self.bare)
            self.assertEqual(merged.returncode, 0)

    def test_pr_mode_leaves_remote_trunk_alone_pushes_the_card_branch_and_asks_gh_for_a_pr(self):
        # Guards: with push = "pr" a landing never moves remote trunk; the merge commit goes up as ade/<card> and
        # gh is asked for a pull request into trunk.
        self.assertEqual(self.ade("push")[0], 0)
        trunk = self.remote("refs/heads/main")
        (self.repo / "pr.txt").write_text("x\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "work")
        sha = self.git("rev-parse", "HEAD").strip()
        bin_dir, log = self.tmp / "bin", self.tmp / "gh.log"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\n[ \"$2\" = view ] && echo '{\"state\":\"OPEN\",\"reviewDecision\":\"CHANGES_REQUESTED\"}' && exit 0\n"
                      "echo \"$@\" > " + str(log) + "\necho https://example.test/pr/1\n")
        gh.chmod(0o755)
        (bin_dir / "gh.cmd").write_text('@if "%2"=="view" (echo {"state":"OPEN","reviewDecision":"CHANGES_REQUESTED"}& exit /b 0)\n'
                                        f"@echo %* > \"{log}\"\n@echo https://example.test/pr/1\n")
        self.git("push", "-q", "origin", f"{trunk}:refs/heads/ade/c8")  # an earlier card's open PR
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        card = {"id": "c9", "title": "t", "done": "it works"}
        env = {"ADE_PROVIDER": "real", "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        with mock.patch.dict(os.environ, env):
            out = land.ship(ctx, card)
        self.assertIn("PR opened: https://example.test/pr/1", out)
        self.assertEqual(self.remote("refs/heads/main"), trunk)
        self.assertEqual(self.remote("refs/heads/ade/c9"), sha)
        self.assertIn("--base main", log.read_text())
        self.assertIn("--head ade/c9", log.read_text())
        # Also guards: an open earlier PR with CHANGES_REQUESTED becomes a residuals row, read-only (gh only viewed it);
        # the card just shipped is not viewed.
        rows = [r["test"] for r in records.read(ctx, "residual")]
        self.assertIn("gh pr view ade/c8", rows)
        self.assertNotIn("gh pr view ade/c9", rows)
        self.assertIn("PR", records.merged(ctx, "card")[land.RESIDUALS]["title"])  # the one residuals card covers PRs

    def test_a_landed_card_is_reshipped_once_with_its_own_commit_only_when_that_never_went_up(self):
        # Guards: a merged card's branch is deleted by design; its commit in remote trunk means nothing to re-ship.
        self.assertEqual(self.ade("push")[0], 0)
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        card = {"id": "c9", "title": "t", "done": "x", "status": "landed", "commit": self.git("rev-parse", "HEAD").strip()}
        note = land.run(ctx, card).note
        self.assertIn("already landed", note)
        self.assertNotIn("never pushed", note)
        self.assertIsNone(self.remote("refs/heads/ade/c9"))
        # Also guards: re-shipping a landed card sends that card's commit, not the checkout's HEAD (a later card's work);
        # a landed row with no commit (null or empty) or an unknown one stays 'already landed'.
        shas = []
        for n in ("c8", "c9"):
            self.git("commit", "-q", "--allow-empty", "-m", n)
            shas.append(self.git("rev-parse", "HEAD").strip())
        bin_dir = self.tmp / "failgh"  # a gh that always fails: no network, and git stays on PATH
        bin_dir.mkdir()
        (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / "gh").chmod(0o755)
        (bin_dir / "gh.cmd").write_text("@exit /b 1\n")
        env = {"ADE_PROVIDER": "real", "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        with mock.patch.dict(os.environ, env):
            for commit in (None, "", "abcdef0123456789"):
                row = {"id": "c7", "title": "t", "status": "landed", "commit": commit}
                self.assertNotIn("never pushed", land.run(ctx, row).note)
            self.assertIsNone(self.remote("refs/heads/ade/c7"))
            row = {"id": "c8", "title": "t", "done": "x", "status": "landed", "commit": shas[0]}
            self.assertIn("never pushed", land.run(ctx, row).note)
        self.assertEqual(self.remote("refs/heads/ade/c8"), shas[0])
        self.assertIsNone(self.remote("refs/heads/ade/c9"))
        # Also guards: under push = "pr" a landing cut off before its push is finished by the next land, once; a repeat
        # changes nothing.
        card = {"id": "c9", "title": "t", "done": "it works", "status": "landed", "commit": shas[1]}
        with mock.patch.dict(os.environ, env):
            self.assertIn("never pushed", land.run(ctx, card).note)
            pushed = self.remote("refs/heads/ade/c9")
            self.assertEqual(pushed, card["commit"])
            self.assertNotIn("never pushed", land.run(ctx, card).note)
            self.assertEqual(self.remote("refs/heads/ade/c9"), pushed)

    def test_a_hand_merged_catch_up_is_recorded_like_adees_own(self):
        # Guards: after a person merges a moved remote trunk by hand, the re-ship appends a landed row naming the
        # merged head and the old commit it caught up from, as catch_up does.
        self.assertEqual(self.ade("push")[0], 0)
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        self.git("commit", "-q", "--allow-empty", "-m", "c9 landed")
        old = self.git("rev-parse", "HEAD").strip()
        other = self.tmp / "ahead"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        for a in (["config", "user.email", "m@x"], ["config", "user.name", "m"],
                  ["commit", "-q", "--allow-empty", "-m", "elsewhere"], ["push", "-q", "origin", "HEAD:main"]):
            subprocess.run(["git", *a], cwd=other, check=True)
        self.git("fetch", "-q", "origin")
        self.git("merge", "--no-ff", "-q", "-m", "by hand", "origin/main")
        bin_dir = self.tmp / "failgh"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / "gh").chmod(0o755)
        (bin_dir / "gh.cmd").write_text("@exit /b 1\n")
        env = {"ADE_PROVIDER": "real", "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        card = {"id": "c9", "title": "t", "done": "d", "status": "landed", "commit": old}
        with mock.patch.dict(os.environ, env):
            land.run(ctx, card)
        rows = [r for r in records.read(ctx, "card") if r.get("id") == "c9" and r.get("status") == "landed"]
        self.assertEqual(rows[-1].get("caught_up_from"), old)
        self.assertEqual(rows[-1]["commit"], self.remote("refs/heads/ade/c9"))

    def _stranded(self, other_file):
        # a landed card c9 pushed as ade/c9 (its own file), then remote trunk moves by committing other_file elsewhere
        self.assertEqual(self.ade("push")[0], 0)
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        (self.repo / "card.txt").write_text("card\n")
        self.git("add", "card.txt")
        self.git("commit", "-q", "-m", "c9 landed")
        old = self.git("rev-parse", "HEAD").strip()
        self.git("push", "-q", "origin", f"{old}:refs/heads/ade/c9")
        other = self.tmp / "ahead"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        (other / other_file).write_text("elsewhere\n")
        for a in (["config", "user.email", "m@x"], ["config", "user.name", "m"], ["add", other_file],
                  ["commit", "-q", "-m", "elsewhere"], ["push", "-q", "origin", "HEAD:main"]):
            subprocess.run(["git", *a], cwd=other, check=True)
        return ctx, {"id": "c9", "title": "t", "done": "d", "status": "landed", "commit": old}, old

    def test_catch_up_merges_trunk_into_the_remote_branch_as_a_fast_forward_and_records_it(self):
        # Guards: --catch-up pushes a merge commit (old tip is its first parent, so never forced or rebased) and the
        # newest landed row names the new tip and the old one it caught up from.
        ctx, card, old = self._stranded("other.txt")
        # Also guards: when another remote ade/* branch contains this head (a stack), it stops and names that branch.
        self.git("commit", "-q", "--allow-empty", "-m", "c10 on top")
        self.git("push", "-q", "origin", "HEAD:refs/heads/ade/c10")
        res = land.run(ctx, card, True)
        self.assertEqual((res.outcome, "ade/c10" in res.note, "c10 --catch-up" in res.next), ("needs-judgement", True, True))
        self.assertEqual(self.remote("refs/heads/ade/c9"), old)
        self.git("push", "-q", "origin", ":refs/heads/ade/c10")
        # Also guards: --catch-up needs only a landed card, a remote and a remote ade/<card>, never [git] push = "pr"
        # (the stop above ran under "pr"; this catch-up runs under "land").
        ctx.cfg["git"]["push"] = "land"
        self.assertEqual(land.run(ctx, card, True).outcome, "done")
        new = self.remote("refs/heads/ade/c9")
        self.assertNotEqual(new, old)
        self.assertEqual(self.git("rev-parse", f"{new}^1").strip(), old)
        self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", self.remote("refs/heads/main"), new],
                                        cwd=self.bare).returncode, 0)
        rows = [r for r in records.read(ctx, "card") if r.get("id") == "c9" and r.get("status") == "landed"]
        self.assertEqual((rows[-1]["commit"], rows[-1].get("caught_up_from")), (new, old))

    def test_catch_up_refuses_on_a_conflict_and_moves_nothing(self):
        # Guards: a conflicting trunk leaves the remote branch where it was and appends no row.
        ctx, card, old = self._stranded("card.txt")
        res = land.run(ctx, card, True)
        self.assertEqual(res.outcome, "needs-judgement")
        self.assertTrue(res.next)
        self.assertEqual(self.remote("refs/heads/ade/c9"), old)
        self.assertFalse([r for r in records.read(ctx, "card") if r.get("caught_up_from")])

    def test_prune_merged_deletes_only_ade_branches_trunk_contains(self):
        # Guards: the one remote deletion ade makes: an ade/* branch whose tip trunk contains goes; an unmerged ade/*
        # branch and a non-ade branch stay; [git] prune_merged = false deletes nothing.
        from ade import push
        self.assertEqual(self.ade("push")[0], 0)
        self.git("branch", "ade/old")
        self.git("commit", "-q", "--allow-empty", "-m", "next")
        self.git("branch", "other/old", "ade/old")
        self.git("checkout", "-q", "-b", "ade/open")
        self.git("commit", "-q", "--allow-empty", "-m", "unmerged")
        self.git("checkout", "-q", "main")
        self.git("push", "-q", "origin", "main", "ade/old", "ade/open", "other/old")
        self.git("branch", "-q", "-D", "ade/open")  # restore: open branch built above is not in main
        self.git("fetch", "-q", "origin", "refs/heads/main")
        self.git("reset", "-q", "--hard", "HEAD")
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["prune_merged"] = False
        self.assertEqual(push.prune_merged(ctx), [])
        self.assertIsNotNone(self.remote("refs/heads/ade/old"))
        ctx.cfg["git"]["prune_merged"] = True
        self.assertEqual(push.prune_merged(ctx), ["refs/heads/ade/old"])
        self.assertIsNone(self.remote("refs/heads/ade/old"))
        self.assertIsNotNone(self.remote("refs/heads/ade/open"))
        self.assertIsNotNone(self.remote("refs/heads/other/old"))

    def test_pr_mode_names_branch_and_title_after_the_id_the_records_push_settled(self):
        # Guards: a card renumbered by the records push (another machine minted its id first) opens its PR under the
        # id it ended with, never the id it was built under.
        self.assertEqual(self.ade("push")[0], 0)
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        self.ade("card", "add", "from B", "--path", "app/calc.py", "--done", "noted", cwd=other)
        self.ade("card", "add", "from A", "--path", "app/calc.py", "--done", "noted")  # same id here: B not pushed yet
        self.assertEqual(self.ade("push", cwd=other)[0], 0)
        old = "c1"  # the first id minted on each machine
        bin_dir, log = self.tmp / "bin", self.tmp / "gh.log"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\necho \"$@\" > " + str(log) + "\necho https://example.test/pr/1\n")
        gh.chmod(0o755)
        (bin_dir / "gh.cmd").write_text(f"@echo %* > \"{log}\"\n@echo https://example.test/pr/1\n")
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        card = {"id": old, "title": "from A", "done": "noted"}
        env = {"ADE_PROVIDER": "real", "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        records.append(ctx, "card", {"id": old, "status": "landed", "commit": "0" * 40})  # a landing whose commit is not the tip
        with mock.patch.dict(os.environ, env):
            land.ship(ctx, card)
        new = ctx.renumbered[old]
        self.assertNotEqual(new, old)
        # Guards: after a renumber the card's latest landed row cites the commit its pushed branch points at.
        self.assertEqual(records.merged(ctx, "card")[new]["commit"], self.remote(f"refs/heads/ade/{new}"))
        self.assertIsNotNone(self.remote(f"refs/heads/ade/{new}"))
        self.assertIsNone(self.remote(f"refs/heads/ade/{old}"))
        self.assertIn(f"--head ade/{new} --title {new}:", log.read_text().replace('"', ""))

    def test_pr_mode_falls_back_to_rest_when_pr_create_is_refused_and_keeps_the_title_short(self):
        # Guards: when `gh pr create` fails, the PR is opened over `gh api` and its html_url is reported; the title
        # given to gh stays short whatever the card title is.
        self.assertEqual(self.ade("push")[0], 0)
        bin_dir, log = self.tmp / "bin", self.tmp / "gh.log"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text('#!/bin/sh\necho "$@" >> ' + str(log) + '\n[ "$1" = api ] && '
                      "echo '{\"html_url\": \"https://example.test/pr/2\"}' && exit 0\necho HTTP 403 >&2\nexit 1\n")
        gh.chmod(0o755)
        reply = '{"html_url": "https://example.test/pr/2"}'
        (bin_dir / "gh.cmd").write_text(f'@echo %* >> "{log}"\n@if "%1"=="api" (echo {reply}& exit /b 0)\n'
                                        "@echo HTTP 403 1>&2\n@exit /b 1\n")
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        card = {"id": "c9", "title": "Long. " + "x" * 200, "done": "it works"}
        env = {"ADE_PROVIDER": "real", "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        with mock.patch.dict(os.environ, env):
            out = land.ship(ctx, card)
        self.assertIn("PR opened: https://example.test/pr/2", out)
        title = [x for x in log.read_text().splitlines() if x.startswith("api")][0].split("title=")[1].split(" -F")[0]
        self.assertLessEqual(len(title), 76)

    def test_pr_mode_card_branch_after_a_remote_merge_carries_only_its_own_work(self):
        # Guards: when the remote merged an earlier ade/<card> into trunk, the next ade/<card> sits on top of remote
        # trunk and holds only its own commits plus the catch-up merge, none of the earlier card's.
        self.assertEqual(self.ade("push")[0], 0)
        bin_dir, log = self.tmp / "bin", self.tmp / "gh.log"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\necho \"$@\" > " + str(log) + "\necho https://example.test/pr/1\n")
        gh.chmod(0o755)
        (bin_dir / "gh.cmd").write_text(f"@echo %* > \"{log}\"\n@echo https://example.test/pr/1\n")
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        env = {"ADE_PROVIDER": "real", "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}

        def land_card(cid):
            (self.repo / (cid + ".txt")).write_text("x\n")
            self.git("add", "-A")
            self.git("commit", "-qm", "work " + cid)
            card = dict(id=cid, title="t", done="d")
            with mock.patch.dict(os.environ, env):
                self.assertIn("PR opened", land.ship(ctx, card))

        land_card("c1")
        other = self.tmp / "merger"  # GitHub's merge: a separate --no-ff merge commit on remote trunk
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        for a in (["config", "user.email", "m@x"], ["config", "user.name", "m"],
                  ["merge", "--no-ff", "-qm", "Merge PR 1", "origin/ade/c1"], ["push", "-q", "origin", "HEAD:main"]):
            subprocess.run(["git", *a], cwd=other, check=True)
        land_card("c2")
        trunk, branch = self.remote("refs/heads/main"), self.remote("refs/heads/ade/c2")
        up = subprocess.run(["git", "merge-base", "--is-ancestor", trunk, branch], cwd=self.bare)
        self.assertEqual(up.returncode, 0)
        subjects = git(self.bare, "log", "--format=%s", trunk + ".." + branch).splitlines()
        self.assertIn("work c2", subjects)
        self.assertNotIn("work c1", subjects)
        self.assertTrue(all(s in ("work c2", "catch up with origin/main") for s in subjects), subjects)

    def test_pr_mode_newest_landed_row_names_the_commit_the_pr_carries_after_a_catch_up(self):
        # Guards: whatever ade/<card> points at on the remote, the card's newest landed row names that same commit,
        # even when open_pr's catch-up merge moved the head past the land commit.
        self.assertEqual(self.ade("push")[0], 0)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\necho https://example.test/pr/1\n")
        gh.chmod(0o755)
        (bin_dir / "gh.cmd").write_text("@echo https://example.test/pr/1\n")
        ctx = config.load(str(self.repo))
        ctx.cfg["git"]["push"] = "pr"
        (self.repo / "c1.txt").write_text("x\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "work c1")
        other = self.tmp / "ahead"  # origin trunk moves on after the land
        subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True)
        for a in (["config", "user.email", "m@x"], ["config", "user.name", "m"],
                  ["commit", "-q", "--allow-empty", "-m", "elsewhere"], ["push", "-q", "origin", "HEAD:main"]):
            subprocess.run(["git", *a], cwd=other, check=True)
        env = {"ADE_PROVIDER": "real", "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        with mock.patch.dict(os.environ, env):
            self.assertIn("PR opened", land.ship(ctx, dict(id="c1", title="t", done="d")))
        rows = [r for r in records.read(ctx, "card") if r.get("id") == "c1" and r.get("status") == "landed"]
        self.assertEqual(rows[-1]["commit"], self.remote("refs/heads/ade/c1"))
