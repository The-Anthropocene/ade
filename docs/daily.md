# The build loop

Published 27/10/2026. Approved by the owner on 27/10/2026 ("yes, daily with a digest"). Continuous building ran from 30/10/2026
until 22:14 that day, when the owner paused it to save CI minutes ("We're running out of ci again so maybe we wait for
the daily"). On 31/10/2026 the owner lifted that daily-only pause ("I extended our ci budget, so clear the backlog").

The loop builds continuously again, refilling the board with small cards whenever it is empty. It pauses only on
"pause", a veto, a red main, an over-share flag, two parks in a row, or a decision that needs the owner. The 06:47
London daily run stays as a heartbeat that restarts the loop if it stopped. Each round flies one small set of cards
from `docs/board.md` (two at most), stacked so one top PR carries the round, and sends the owner one short digest with
a veto. It is this team's own loop for building ADE, not a feature of the kit: the kit's charter still leaves
unattended runs to each user, and nothing here ships.

Think of it as an overnight shift: one job, done properly, with a handover note on the desk in the morning. The note
always says what to undo if the owner doesn't like it.

## One cycle

1. **Sense.** Fetch main. Read the last runs of `test`, `site`, `version` and `package`, open PRs, the board, and
   any reply the owner sent since the last digest.
2. **Stop early** (and say why in the digest) when any of these holds:
   - the owner wrote "pause" (until they write "resume");
   - (not a stop) stacked PRs are fine: one per card, each with a landed card;
   - main is red: that day's card is fixing main instead;
   - the week meter says the provider is over its share (see "Meters" below);
   - two cards in a row parked: something is wrong with the loop, so it asks instead.
3. **Pick** the first two `open` cards on the board.
4. **Build** them with `ade conduct --lanes 2`, which flies a second card beside the first only when their reach in the
   graph does not meet, and otherwise after it. In the cloud the session works in a real clone of origin with
   `[permissions] lane_mode = "acceptEdits"` and `[git] push = "pr"` set in that clone only, uncommitted (bypass is
   refused in the container), adds each board card with `ade card add`, and flies them with `ade conduct --lanes 2`. A
   lane cannot run shell commands: regenerate what `ade init` owns afterwards. Each landing pushes `ade/<card>` and the
   `ade-records` branch. Each card is the smallest change that meets its done line, under the usual rules
   (`AGENTS.md`, stdlib only, cross-platform, the minimal-tests policy). Run the gates the review thread runs:
   `python -m unittest`, `ade budget`, `ade graph --check`. The same PR sets its cards to `done`.
5. **Open the PRs**: one per card with head `ade/<card>`, through GitHub (there is no `gh` in the container), and tell
   the review thread their numbers. The review thread merges only a PR whose head is `ade/<card>` and whose card has a
   landed row on origin's `ade-records`; CI then ships
   whatever the merge touches (`site` deploys the website, `version` releases a new version).
6. **Digest.** One message per round to the owner, under 80 words: what was built and its PR, what shipped since the last
   digest, anything parked, open questions (each with its recommendation, default and deadline), and the veto line.

## Veto

Replying "veto" (or "veto #N") reverts that PR with a new revert PR, which the review thread merges like any other.
The card goes back to `open` with the reason, or to `parked` if the owner says to drop it.

## Questions

Every question to the owner carries a recommendation, a default and a deadline, and silence takes the default when
the deadline passes. Inside the kit this is `ade decide --ask`; in the project it is a decision card whose text
states the default and the deadline.

## What stays with the owner

Money, accounts, tokens and secrets, signatures, and public claims (pricing, licence wording, anything said about a
named person or company). The build never makes these changes and never guesses at them. It asks once, with a
default that leaves things as they are.

## Meters

The week meters live on the machine that reads them, and their numbers never leave it. The owner chose (27/10/2026)
to let one thing out: a yes/no flag per provider, "over its share until <reset>", recorded on the `ade-records` branch
when the flag changes and pushed with it (card M1). The build reads the newest flag for its provider from
`origin/ade-records` and stops early while it is set and its reset is still ahead. No flag, or one whose reset has
passed, means go.

## Windows CI switch

November's free CI minutes ran out (c342), so Windows CI is off by default: `.github/workflows/test.yml` runs Windows only for release tags and manual runs. To turn the 1 Nov policy back on (Windows for proof-grade PRs, the nightly run, and `ade init --dry-run` plus `ade rehearse` on every PR), set the repository variable `ADE_WINDOWS_CI` to `on` (Settings, Secrets and variables, Actions, Variables). Delete it, or set it to anything else, to turn it off again. No file changes either way. Drafts and stacked intermediate PRs are always skipped.

Windows minutes are free on the self-hosted PC runner (label `ade-pc`). Set the repository variable `ADE_WINDOWS_RUNNER` to `pc` and the Windows job runs there (never for fork PRs), and also runs wherever `ADE_WINDOWS_CI=on` would (proof-grade PRs, the nightly run); the Linux full and rehearse path still waits on `ADE_WINDOWS_CI`. Delete the variable to return to hosted `windows-latest`. With the PC offline, Windows jobs just queue: there is no automatic fallback, so unset the variable to go back to hosted.

The Linux jobs (test, package, version tag, site deploy) can run on a self-hosted Linux runner too (label `ade-linux`). Set the repository variable `ADE_LINUX_RUNNER` to `pc` and they run there (never for fork PRs); delete it to return to hosted `ubuntu-latest`. With the PC offline, Linux jobs just queue: there is no automatic fallback, so unset the variable to go back to hosted.
