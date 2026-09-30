"""A content-addressed repository: every version is named by the hash of its contents.

A version is an immutable mapping path -> text. Its identifier is a SHA-256 over the
sorted (path, text) pairs, so identical contents always have the same identifier
and any change yields a new one, the way a git commit is named. Old versions stay
readable; only `head`, the identifier of the current version, moves.

Writes here are deliberately naive: a write replaces one file's full text in the
current head, whatever version the writer based its change on. A writer that read
an older version therefore silently discards changes made since (a lost update).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


def version_id(files: dict[str, str]) -> str:
    blob = json.dumps(sorted(files.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Commit:
    version: str
    parent: str | None
    author: str
    note: str


class Repo:
    def __init__(self, files: dict[str, str]):
        self._versions: dict[str, dict[str, str]] = {}
        self.head = self._store(files)
        self.log: list[Commit] = [Commit(self.head, None, "init", "initial version")]

    def _store(self, files: dict[str, str]) -> str:
        vid = version_id(files)
        self._versions[vid] = dict(files)
        return vid

    def files(self, version: str | None = None) -> dict[str, str]:
        return dict(self._versions[version or self.head])

    def read(self, path: str, version: str | None = None) -> str:
        return self._versions[version or self.head][path]

    def write(self, path: str, text: str, author: str, note: str = "") -> str:
        """Naive write: replace `path` in the CURRENT head, whatever the writer read."""
        return self.write_files({path: text}, author, note)

    def write_files(self, changes: dict[str, str], author: str, note: str = "") -> str:
        """Naive multi-file write, applied to the current head as one new version."""
        files = self.files()
        files.update(changes)
        parent, self.head = self.head, self._store(files)
        self.log.append(Commit(self.head, parent, author, note))
        return self.head
