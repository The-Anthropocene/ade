# Testing: proof of done, and kept tests

Published 30/10/2026.

Two different things get called testing. Keep them apart.

## Proof of done

A card is done when its done line is shown, not when a test exists. Pick the proof that fits the card:

- **A command and its output.** Run it (`ade q run -- <command>`) and quote the exit code and the relevant lines.
- **A page that loads.** Build the page and open it; say what you saw.
- **A figure with its denominator.** "12 of 40 cards" or "3 s over 20 runs", never a bare "faster" or "12".
- **The owner's recorded feedback.** A decision or note in the records, for what only a person can judge.

Proof is evidence for this card, once. It is not a test, and it is not kept.

## When a test is kept

A test stays in the suite only if all four hold:

1. **It guards a lasting hazard**, named in one sentence in a comment above it.
2. **It asserts an outcome or a refusal code**, never copied text, counts or lists.
3. **It was seen red with the fix taken out**, so it is known to catch the hazard.
4. **It runs where it belongs**: a fast invariant in the default suite, anything slow or platform-bound in its own tier.

If one fails, show the proof on the card and do not keep the test. Fewer tests are better.

## ADE examples

A date's format is a lasting truth; today's date is not:

```python
# Guards: records are dated dd/mm/yyyy, so readers can parse them.
assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", row["date"])
# Never: assert row["date"] == "30/10/2026"   (true only today)
```

Likewise assert the refusal code a bad input raises (`tests.protected`), not the wording of its message, and assert a property ("every need names a known part"), not the list of parts.

## Files that must move together

A schema change that must regenerate a client is a test command, not a kit feature. Put the regeneration and a clean-diff check in `always`:

```toml
always = ["sh -c 'make gen && git diff --exit-code gen/'"]
```

If the generated files are stale, the diff is not clean and the test stage fails. For a one-off coupling, name the same command as a card proof instead. There is no `[couple]` table.

## Hear about stops without watching

`ade conduct --follow --json` prints one JSON line for each card it parks, among its ordinary progress lines. Pipe it to your own notifier, skip what is not JSON and filter on `outcome`, which reads `parked`:

```sh
ade conduct --follow --json | python -c '
import json, sys
for line in sys.stdin:
    if not line.startswith("{"):
        continue  # a progress line, not a stop
    row = json.loads(line)
    if row.get("outcome") == "parked":
        print(line.strip(), flush=True)  # replace with your own mail, chat or push call
'
```

The line carries the card, the stage, the next command and the open question with its recommendation, default and deadline.

Nothing in the kit sends messages; the notifier is yours.
