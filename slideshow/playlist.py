"""In-memory shuffle cycles with bounded actual-playback history."""
import random


class ShufflePlaylist:
    def __init__(self, ids, current=None):
        self.known = set()
        self.pending = []
        self.seen = set()
        self.history = []
        self.cursor = -1
        self.sync(ids)
        if current in self.known:
            self.select(current)

    def sync(self, ids):
        available = set(ids)
        if available == self.known:
            return
        added = available - self.known
        self.known = available
        self.pending = [item for item in self.pending if item in available]
        new = [item for item in ids if item in added and item not in self.seen]
        if new:
            self.pending.extend(new)
            random.shuffle(self.pending)
        # Retain the cursor position while deleting unavailable history entries.
        before = self.history[:self.cursor + 1]
        self.history = [item for item in self.history if item in available]
        self.cursor = sum(item in available for item in before) - 1
        if len(self.seen) > 20000:
            self.seen.intersection_update(available)

    def remap(self, changes):
        self.pending = [changes.get(item, item) for item in self.pending]
        self.history = [changes.get(item, item) for item in self.history]
        self.seen = {changes.get(item, item) for item in self.seen}
        self.known = {changes.get(item, item) for item in self.known}

    def remember(self, item):
        if self.cursor + 1 < len(self.history):
            del self.history[self.cursor + 1:]
        self.history.append(item)
        if len(self.history) > 20000:
            del self.history[:-20000]
        self.cursor = len(self.history) - 1
        self.seen.add(item)
        return item

    def select(self, item):
        # Explicit Show is allowed to revisit a photo, but consumes it if unseen.
        if item in self.pending:
            self.pending.remove(item)
        if self.cursor >= 0 and self.history[self.cursor] == item:
            return item
        return self.remember(item)

    def next(self):
        if not self.known:
            return None
        if self.cursor + 1 < len(self.history):
            self.cursor += 1
            return self.history[self.cursor]
        if not self.pending:
            self.pending = sorted(self.known)
            random.shuffle(self.pending)
            previous = self.history[self.cursor] if self.cursor >= 0 else None
            if len(self.pending) > 1 and self.pending[0] == previous:
                # Choose a different first item without changing cycle membership.
                other = random.randrange(1, len(self.pending))
                self.pending[0], self.pending[other] = self.pending[other], self.pending[0]
            self.seen.clear()
        return self.remember(self.pending.pop(0))

    def previous(self):
        if self.cursor > 0:
            self.cursor -= 1
        return self.history[self.cursor] if self.cursor >= 0 else None
