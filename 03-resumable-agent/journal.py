"""An append-only, fsync'd journal: the run's stable storage.

One JSON record per line. A record is durable once `append` returns: the line is
written, flushed, and fsync'd before the caller proceeds. A crash can therefore
leave at most one incomplete line, at the very end (a torn write), which `load`
discards. Damage anywhere else is corruption and is reported, not repaired.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class CorruptJournal(Exception):
    pass


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        raw = self.path.read_text(encoding="utf-8")
        lines = raw.split("\n")
        # A well-formed journal ends with "\n", so the last split element is "".
        # Anything else in that position is a torn final write.
        complete, torn = lines[:-1], lines[-1]
        records = []
        for n, line in enumerate(complete, 1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise CorruptJournal(f"{self.path}: line {n} is not a valid record") from e
        if torn:
            # Drop the partial line so later appends start on a clean boundary.
            good = "".join(l + "\n" for l in complete)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(good, encoding="utf-8")
            os.replace(tmp, self.path)
        return records
