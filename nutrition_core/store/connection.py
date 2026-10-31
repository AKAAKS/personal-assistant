"""Abstract database connection interface used by the nutrition persistence repos.

The repositories take a connection object as a parameter instead of opening one,
so this module only describes the surface they rely on. ``sqlite3.Connection``
satisfies ``DbConnection`` structurally, and a libSQL connection can satisfy it
later without changes to the repos. Rows are accessed by column name, so callers
rely on ``row_factory = sqlite3.Row`` (set by ``jarvisd.db.connect``).

No DB is opened and nothing is registered or subclassed; importing this module
has no side effects.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class DbConnection(Protocol):
    def execute(self, sql: str, params: Sequence = ()) -> Any: ...

    def executescript(self, sql: str) -> Any: ...

    def commit(self) -> None: ...
