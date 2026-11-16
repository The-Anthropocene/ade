# Keeping a person's question for the person

Internal design note, not a site page. Exploration; nothing here is built.

## Premise
With `[conduct] answerer = "person"` (the template default, `ade/template.toml:185`),
nothing stops the agent that drives ADE from running `ade decide --answer ID TEXT`
itself. The park text only asks in words: "a person answers: ade decide --answer
dN retry". `ade.toml` is protected, so the agent cannot flip `answerer`, but it
does not need to: it can type the answer.

## What the code does today

- `ade/verbs/decide.py` `--answer`: refuses a retired ruling or a missing
  question/text, then appends `{"id", "answer", "answered": <date>}`
  (`decide.py:190-199`). The row records no caller: no user, no session, no
  "agent or person" field. `state()` then reads the question as `answered`.
- `decide.park()` (`decide.py:25`) writes the question and a second row naming
  who answers, chosen from `[conduct] answerer`: "agent" says the watching agent
  answers; anything else says "a person answers".
- `ade/verbs/conduct.py` `_park()` (`:68`) appends a `hold` row (`kind: person`),
  parks the question with `default: retry` and a deadline of today, prints
  "parked", and returns; the round carries on with other cards. `waiting()`
  (`:79`) keeps the card down while its question is open or answered with
  anything but retry/accept/land/rework. A question past its deadline takes its
  default, so the next day's run retries it.
- `ade/verbs/hook.py`: hook 2 (`_bash`, PreToolUse on `Bash`) runs `guard.check`;
  a `Refusal` becomes `permissionDecision: "deny"` with the reason
  (`hook.py:371-373`). The hook code emits only `deny` and `additionalContext`;
  it never emits `ask`.
- `ade/install.py`: `HOOKS` (`:21`) goes to Claude Code's settings. For Codex,
  `codex_hooks()` writes `.codex/hooks.json` with the grep note (a PostToolUse
  entry) alone, and it runs only once trusted by hand. For opencode, `install.py`
  writes no hook at all. So only Claude Code has a PreToolUse Bash guard.

## Options

### (a) The Bash hook returns "ask" for `ade decide --answer`

- Stops: the agent answering silently; the person sees the command and clicks.
- Costs: the agent's turn is held until someone clicks. With no person around,
  progress stalls (the owner's caveat). It also asks when the person told the
  agent to answer.
- Fails: the unattended user (a stalled loop overnight); a person who clicks
  yes by habit.
- Needs a new `ask` path in the hook. Probe: does Claude Code honour `ask` from
  PreToolUse in every permission mode, bypassPermissions included?

### (b) The hook denies it; the person answers outside the agent

- Stops: the agent answering through a Bash call the hook sees. The deny
  reason says "a person answers in their own shell", so it moves on (see d).
- Costs: the person must open a shell and type the command: friction.
  A deny is a match on the command line: it must see through chains, `sh -c` and
  scripts. The real hole is a direct append to `.ade/records.jsonl`, plain text
  the agent can write. Probe: does `guard` already cover that file, and are the
  Write/Edit tools open on it?
- Fails: Codex and opencode (no PreToolUse hook installed); the person who has
  no shell on the machine (a hosted session). Probe: does the person's own
  terminal pass the agent's hooks? Hooks belong to the agent's session, so a
  separate shell should not, but confirm it with a run.
- Unconfirmed: an environment marker telling `decide` it runs inside an agent.
  Probe: which variables each engine sets for child processes.

### (c) No prevention; record the source and show it

- Stops: nothing. Makes an agent-given answer visible afterwards.
- Costs: `decide --answer` adds one field (`by`) to the answer row, from an agent
  marker in the environment (needs the probe above); the briefing marks "d7:
  answered by an agent, a person's question". No new record type, no stall.
- Fails: the answer has already applied (a land question answered `land` has
  landed). The marker is only as honest as the environment: an agent can unset a
  variable. It catches the careless case, not the determined one. Where no
  marker exists (opencode, unknown wrappers) it reads "person" wrongly, which is
  worse than saying "unknown".

### (d) Conduct parks and moves on

- Already true: `_park()` records the question and returns "parked"; the other
  cards fly (`conduct.py:68-77`). So (b) and (c) do not stall the loop; only (a)
  stalls, because it holds a single agent turn and not the board.
- Stops: nothing about who answers. It limits the cost of an unanswered question
  to one card, and the default (retry, deadline today) reopens it next day.
- Fails: a card needing a real person's call waits, or retries into the same
  stop each day unseen.

### (e) Reach an absent person

- Stops: nothing. Shortens the wait, so fewer answers are tempting to give.
- Costs: a notification channel (push, mail, chat): a network call and a
  setting. Sending a question's text leaves the machine, so opt-in in
  `ade.toml`, never default.
- Fails: a person with no phone near; a notification the agent could read.

## Engines
| option | Claude Code | Codex | opencode |
| --- | --- | --- | --- |
| a ask | yes, after a probe | no hook installed | no hook installed |
| b deny | yes, shell-visible commands only | no | no |
| c record | yes, if a marker is set | marker unknown | marker unknown |
| d park | all (conduct is ADE's own) | all | all |
| e notify | per channel | per channel | per channel |

## Recommendation
Do (c) first, with (d) as it stands, and add (b) for Claude Code only after a
probe. Reason: (c) is the only option that works on all three engines and costs
no stall, and it makes the failure visible instead of claiming a guard ADE cannot
keep. (a) is rejected as the default: it turns an unattended board into a
stalled one. Offer it as an opt-in for people who sit with the agent.

## Smallest first card

"Decide records who answered": `ade decide --answer` stores `by` (`agent` when an
agent marker is in the environment, `person` when a terminal is attached and none
is, else `unknown`), and the briefing line for a question answered by an agent
under `answerer = "person"` says so. Paths: `ade/verbs/decide.py`,
`ade/verbs/hook.py` (briefing). Before building: a probe card that prints the
environment each engine gives a child process.
