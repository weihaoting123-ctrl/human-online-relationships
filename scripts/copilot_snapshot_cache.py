"""Grant-local verified source copies. No original database is opened for writing."""
from contextlib import contextmanager
from pathlib import Path


class SnapshotCache:
    """Reuse individual verified shards; byte changes replace only their copy."""

    def __init__(self, sync):
        self.sync = sync
        self.slots = {}
        self.checked = {}
        self.fingerprints = {}

    def source_fingerprint(self, source):
        source = Path(source).resolve(strict=True)
        if source not in self.checked:
            self.checked[source] = self.sync._stable_database_fingerprint(source)
        return self.checked[source]

    def path_for(self, source):
        source = Path(source).resolve(strict=True)
        current = self.source_fingerprint(source)
        old = self.slots.get(source)
        if old is not None and old.fingerprints[source] == current:
            self.fingerprints[source] = current
            return old.path_for(source)
        # Build and validate before replacing a previously usable private copy.
        # A refresh error propagates: stale data is never labelled current.
        fresh = self.sync._DatabaseSnapshotSet()
        try:
            target = fresh.path_for(source)
        except BaseException:
            fresh.close()
            raise
        self.slots[source] = fresh
        self.fingerprints[source] = self.checked[source] = fresh.fingerprints[source]
        if old is not None:
            old.close()
        return target

    @contextmanager
    def scope(self):
        self.checked = {}
        self.fingerprints = {}
        token = self.sync._ACTIVE_DATABASE_SNAPSHOTS.set(self)
        try:
            yield self
        finally:
            self.sync._ACTIVE_DATABASE_SNAPSHOTS.reset(token)
            for source in set(self.slots) - set(self.checked):
                self.slots.pop(source).close()
            self.checked = {}

    def close(self):
        for snapshot in self.slots.values():
            snapshot.close()
        self.slots.clear()
        self.checked.clear()
        self.fingerprints.clear()
