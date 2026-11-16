# id: ade.setup
# what: the state of the `ade setup` panel, pure: cursor, which parts are off, the warnings that costs, and the ade.toml diff saving would make
# cmd: -
# reads: ade/parts.toml
# writes: -
import difflib

from ade import parts


class Panel:
    def __init__(self, cat, off, why):
        self.cat, self.off, self.why = cat, set(off), dict(why)
        self.ids, self.at = list(cat), 0

    def move(self, step):
        self.at = (self.at + step) % len(self.ids)

    def cycle(self):
        """Jump to the first part of the next group."""
        group = self.cat[self.ids[self.at]]["group"]
        for i in range(1, len(self.ids) + 1):
            j = (self.at + i) % len(self.ids)
            if self.cat[self.ids[j]]["group"] != group:
                self.at = j
                return

    def toggle(self, pid=None):
        """Flip a part (the one under the cursor by default) exactly as `ade parts set` does."""
        pid = pid or self.ids[self.at]
        if pid in self.off:
            self.off.discard(pid)
            self.why.pop(pid, None)
        else:
            self.off.add(pid)

    def warnings(self):
        return [w for pid in sorted(self.off) for w in parts.warnings(self.cat, pid, self.off)]

    def new_text(self, text):
        return parts.rewrite(text, self.off, self.why)

    def diff(self, text):
        return "".join(difflib.unified_diff(text.splitlines(True), self.new_text(text).splitlines(True), "ade.toml", "ade.toml (new)"))
