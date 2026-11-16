# Known gaps (internal, not a site page)
Add an entry at the top: date found, what a user hits, mitigation (or "none"), link to the card or note. A closed gap moves to "Closed" with its card.

## Open

### Agent can answer a person's question (found 6 Nov 2026)
- Hit: with `answerer = "person"`, the agent driving ADE can still run `ade decide --answer` itself; only the park text asks it not to.
- Mitigation: the park text asks; c595 records `by` (agent, person, unknown) and `engine` on the answer row from engine environment markers, shows it in the session briefing and `ade telemetry --outcomes`. The marker catches the careless case, not a determined agent (it can unset a variable).
- Link: docs/person-answers.md

### Hooks and settings load only from the project folder (found 6 Nov 2026)
- Hit: ADE's hooks and settings are project files, so an agent session started outside the project folder does not load them until restarted there.
- Mitigation: `ade init` now prints a line telling you to start the agent from the project folder (c569). It is a warning, not a fix.
- Link: c569.

### Two rulings added at the same moment can both pass the limit (found 13 Nov 2026)
- Hit: `ade decide` reads the ruling count before it takes the write lock, so two adds at the same moment can both pass `[budgets] rulings` and leave the book one over.
- Mitigation: a book over its limit keeps every ruling and takes no new one; `ade decide --rulings` shows the counts.

### A file that does not exist yet meets a narrower ruling only at its first edit (found 13 Nov 2026)
- Hit: the builder prompt matches rulings against the card's globs and the tracked files under them, so a file the card will create under a narrower ruling's glob is not matched in the prompt.
- Mitigation: the first-edit brief names that ruling when the builder first edits the file.

## Closed
None yet.
