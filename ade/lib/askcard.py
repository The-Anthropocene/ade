# id: ade.lib.askcard
# what: warn when a decide --ask text names a card but --card is missing, so the question could never settle on close
# cmd: -
# reads: -
# writes: -
import re
import sys


def warn_unlinked(text, card):
    m = None if card else re.search(r"\bc\d+\b", text or "")
    if m:
        c = m.group()
        print(f"note: this question names {c} but has no --card; pass --card {c} so it settles when {c} lands or closes", file=sys.stderr)
