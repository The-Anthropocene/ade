# Known gaps
Add an entry at the top: date found, what a user hits, mitigation (or "none"), and a link to a note if there is one. A closed gap moves to "Closed" with the change that closed it.

## Open

### Agent can answer a person's question (found 6 Nov 2026)
- Hit: with `answerer = "person"`, the agent driving ADE can still run `ade decide --answer` itself; only the park text asks it not to.
- Mitigation: the park text asks; the answer row records `by` (agent, person, unknown) and `engine` from engine environment markers, and the session briefing and `ade telemetry --outcomes` show it. The marker catches the careless case, not a determined agent (it can unset a variable).
- Link: ARCHITECTURE.md 3.5.

### Hooks and settings load only from the project folder (found 6 Nov 2026)
- Hit: ADE's hooks and settings are project files, so an agent session started outside the project folder does not load them until restarted there.
- Mitigation: `ade init` prints a line telling you to start the agent from the project folder. It is a warning, not a fix.

### A file that does not exist yet meets a narrower ruling only at its first edit (found 13 Nov 2026)
- Hit: the builder prompt matches rulings against the card's globs and the tracked files under them, so a file the card will create under a narrower ruling's glob is not matched in the prompt.
- Mitigation: the first-edit brief names that ruling when the builder first edits the file.

## Closed

### Two rulings added at the same moment can both pass the limit (found 13 Nov 2026)
- Hit: `ade decide` reads the ruling count before it takes the write lock, so two adds at the same moment can both pass `[budgets] rulings` and leave the book one over.
- Closed: `records.append` takes a `guard` run under its write lock before the write, and `ade decide` passes its fit() check as that guard, so the count is read under the lock.
