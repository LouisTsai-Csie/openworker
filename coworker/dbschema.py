"""Schema generation stamped on every SQLite file, and the refusal that protects it.

ARCHITECTURE.md §7 D7 names three things: 26 `CREATE TABLE` sites, 25 ad-hoc
`ALTER TABLE`s, and `PRAGMA user_version` never set. This module fixes the third, which
is the one that costs data — "a downgrade corrupts".

The failure, concretely. A user runs a build that adds a column, then rolls back to the
previous build. The old build opens the newer file, finds its own `CREATE TABLE IF NOT
EXISTS` satisfied, runs its `ALTER TABLE` list (every one a no-op, because the columns are
already there), concludes the file is fine, and starts writing rows against a schema it
does not know. Nothing anywhere notices. `PRAGMA user_version` is 0 on every file we have
ever written, so there is no fact on disk to notice *with*.

`ensure_version` gives the file that fact and makes the old build stop instead.

**It is a guard, not a migration system.** It answers "may this build open this file?" and
nothing else. It does not run migrations, and deliberately: there are no migrations to run
today — every store converges its own schema with `CREATE TABLE IF NOT EXISTS` plus
`ALTER TABLE ... ADD COLUMN` swallowing "duplicate column", which is why rolling *forward*
has always worked. Only rolling back was unprotected. Picking a migration tool is a
separate decision, and a better one to make once the stores share a base class (D8) and
there is one place to put it; whatever gets chosen then subsumes this guard rather than
replacing it.

**One version for every file, not one per store.** `PRAGMA user_version` is a single
integer per *file*, and `coworker.db` is written by five stores — conversations, memory,
audit, projects, machines — that are constructed independently and in no fixed order. None
of them can own that integer, so no per-store numbering fits. `SCHEMA_VERSION` below is
therefore one number covering every database this product writes: bump it when *any*
store's schema changes, and every file moves together. Coarse, and fine for a guard — the
question it answers is about the build, not about one table.

Raising it is not a one-line change; see the comment on the constant.

This is L0: it imports nothing from `coworker`.
"""

from __future__ import annotations

import sqlite3

# The schema generation this build writes, shared by every SQLite file under the state
# directory. 1 is "everything that shipped before schema versioning existed" — the current
# converged schema, which the stores' own DDL already produces from a file of any age, so
# existing files are stamped where they stand and nothing is rewritten.
#
# BEFORE RAISING THIS: a file stamped with an older version has no upgrade path, and
# `ensure_version` refuses it rather than guessing (see `SchemaOutOfDate`). Bumping this
# means deciding what that path is, which is the point at which this product needs a real
# migration tool rather than a number.
SCHEMA_VERSION = 1

_UNVERSIONED = 0


class SchemaTooNew(RuntimeError):
    """The file was written by a newer build. Opening it would corrupt it."""

    def __init__(self, path: str, found: int, supported: int) -> None:
        self.path, self.found, self.supported = path, found, supported
        super().__init__(
            f"{path} was written by a newer version of OpenWorker (schema {found}; this "
            f"build understands {supported}). Opening it with an older build writes rows "
            f"the newer one cannot read back. Re-install the newer version, or move the "
            f"state directory aside to start fresh."
        )


class SchemaOutOfDate(RuntimeError):
    """The file predates this build's schema and nothing here knows how to upgrade it.

    Unreachable while `SCHEMA_VERSION` is 1: there is no version between "unstamped" and
    1. It exists so that raising `SCHEMA_VERSION` fails loudly at the first file it meets
    instead of silently restamping one that was never upgraded.
    """

    def __init__(self, path: str, found: int, wanted: int) -> None:
        self.path, self.found, self.wanted = path, found, wanted
        super().__init__(
            f"{path} is at schema {found} and this build writes {wanted}, but no upgrade "
            f"path from {found} exists. Whoever raised SCHEMA_VERSION to {wanted} owes "
            f"this file a migration (coworker/dbschema.py)."
        )


def schema_version(conn: sqlite3.Connection) -> int:
    """The generation stamped on this file. 0 means nothing has stamped it yet."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else _UNVERSIONED


def ensure_version(conn: sqlite3.Connection, path: str | None = None) -> int:
    """Refuse a file this build must not touch; stamp one that has never been stamped.

    Call it immediately after `sqlite3.connect`, **before** any DDL — the whole value is
    in stopping before the old build's DDL and writes reach a newer file.

    Returns the version now on the file. Raises `SchemaTooNew` for a file from the future
    and `SchemaOutOfDate` for one this build cannot bring forward.
    """
    found = schema_version(conn)
    if found > SCHEMA_VERSION:
        raise SchemaTooNew(path or _describe(conn), found, SCHEMA_VERSION)
    if found != _UNVERSIONED and found < SCHEMA_VERSION:
        raise SchemaOutOfDate(path or _describe(conn), found, SCHEMA_VERSION)
    if found == _UNVERSIONED:
        # An existing file is already current: every store runs convergent DDL on open, so
        # a file of any age holds today's schema by the time anything reads it. Stamp it
        # where it stands. PRAGMA takes no bind parameters; the value is our own constant.
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.commit()
    return SCHEMA_VERSION


def _describe(conn: sqlite3.Connection) -> str:
    """The file behind a connection, for the error message. ':memory:' has no path."""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main":
                return str(file) or ":memory:"
    except sqlite3.Error:
        pass
    return "this database"
