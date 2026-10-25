"""Every SQLite file carries a schema generation, and an older build refuses a newer file.

ARCHITECTURE.md §7 D7's cost line is "a downgrade corrupts". The mechanism of that
corruption is specific and worth stating, because it is what these tests reproduce: an
older build opens a file written by a newer one, finds its `CREATE TABLE IF NOT EXISTS`
already satisfied and every `ALTER TABLE ... ADD COLUMN` a no-op, concludes the file is
fine, and starts writing. `PRAGMA user_version` was 0 on every file we ever wrote, so
nothing on disk could contradict it.

These tests are about the refusal, not about migrations — there is no migration mechanism
here and deliberately so (see coworker/dbschema.py).
"""

from __future__ import annotations

import sqlite3

import pytest

from coworker import dbschema
from coworker.audit import AuditStore
from coworker.dbschema import (
    SCHEMA_VERSION,
    SchemaOutOfDate,
    SchemaTooNew,
    ensure_version,
    schema_version,
)
from coworker.conversations import ConversationStore
from coworker.memory.sqlite_store import SQLiteMemoryStore
from coworker.teams.chat import ChatStore


def _fresh(tmp_path, name="x.db"):
    return sqlite3.connect(tmp_path / name)


# -- the stamp --------------------------------------------------------------------
def test_an_unstamped_file_is_stamped_where_it_stands(tmp_path):
    conn = _fresh(tmp_path)
    assert schema_version(conn) == 0, "sqlite defaults user_version to 0"
    assert ensure_version(conn) == SCHEMA_VERSION
    assert schema_version(conn) == SCHEMA_VERSION


def test_the_stamp_survives_reopening(tmp_path):
    ensure_version(_fresh(tmp_path))
    assert schema_version(_fresh(tmp_path)) == SCHEMA_VERSION


def test_stamping_is_idempotent(tmp_path):
    conn = _fresh(tmp_path)
    assert ensure_version(conn) == ensure_version(conn) == SCHEMA_VERSION


def test_an_existing_file_keeps_its_data(tmp_path):
    """Stamping must not rewrite anything — existing state dirs are stamped in place."""
    conn = _fresh(tmp_path)
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.execute("INSERT INTO t VALUES ('keep me')")
    conn.commit()

    ensure_version(conn)

    assert conn.execute("SELECT a FROM t").fetchone()[0] == "keep me"


# -- the refusal ------------------------------------------------------------------
def test_a_file_from_a_newer_build_is_refused(tmp_path):
    conn = _fresh(tmp_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(SchemaTooNew) as caught:
        ensure_version(conn)

    assert caught.value.found == SCHEMA_VERSION + 1
    assert caught.value.supported == SCHEMA_VERSION


def test_the_refusal_names_the_file_and_says_what_to_do(tmp_path):
    conn = _fresh(tmp_path, "coworker.db")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 9}")

    with pytest.raises(SchemaTooNew) as caught:
        ensure_version(conn)

    message = str(caught.value)
    assert "coworker.db" in message, "a user has to know which file to move aside"
    assert "newer version" in message and "Re-install" in message


def test_a_refused_file_is_not_restamped(tmp_path):
    """The guard must not write to the file it is refusing."""
    conn = _fresh(tmp_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()

    with pytest.raises(SchemaTooNew):
        ensure_version(conn)

    assert schema_version(_fresh(tmp_path)) == SCHEMA_VERSION + 1


def test_a_file_below_this_build_refuses_rather_than_restamping(tmp_path, monkeypatch):
    """Unreachable at SCHEMA_VERSION 1 — there is no version between unstamped and 1.

    It is the landmine for whoever raises SCHEMA_VERSION: an old file must fail loudly
    instead of being quietly restamped as if it had been upgraded.
    """
    conn = _fresh(tmp_path)
    ensure_version(conn)  # stamped at 1
    monkeypatch.setattr(dbschema, "SCHEMA_VERSION", SCHEMA_VERSION + 1)

    with pytest.raises(SchemaOutOfDate) as caught:
        ensure_version(conn)

    assert caught.value.found == SCHEMA_VERSION
    assert "owes this file a migration" in str(caught.value)
    assert schema_version(conn) == SCHEMA_VERSION, "a file it cannot upgrade stays untouched"


# -- the stores actually use it ---------------------------------------------------
def test_every_store_stamps_the_file_it_opens(tmp_path):
    AuditStore(tmp_path / "audit.db")
    ConversationStore(tmp_path / "state")
    SQLiteMemoryStore(tmp_path / "mem.db")
    ChatStore(tmp_path / "chat.db")

    for name in ("audit.db", "state/coworker.db", "mem.db", "chat.db"):
        assert schema_version(sqlite3.connect(tmp_path / name)) == SCHEMA_VERSION, name


def test_a_store_refuses_a_database_from_the_future(tmp_path):
    """The end-to-end case: the old build stops instead of writing into a newer file."""
    path = tmp_path / "audit.db"
    AuditStore(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaTooNew):
        AuditStore(path)


def test_the_guard_runs_before_the_stores_own_ddl(tmp_path):
    """Order is the whole point: refusing after the DDL has already run is no refusal.

    A future file whose table this build does not know must come back untouched — no new
    column, no new table, nothing.
    """
    path = tmp_path / "audit.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY, brand_new TEXT)")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaTooNew):
        AuditStore(path)

    after = sqlite3.connect(path)
    columns = {row[1] for row in after.execute("PRAGMA table_info(audit_events)")}
    assert columns == {"id", "brand_new"}, f"the refused build still altered the table: {columns}"


def test_stores_sharing_one_file_agree_on_its_version(tmp_path):
    """coworker.db is written by five stores; one integer covers the file, not each store."""
    base = tmp_path / "state"
    ConversationStore(base)
    AuditStore(base / "coworker.db")
    SQLiteMemoryStore(str(base / "coworker.db"))

    assert schema_version(sqlite3.connect(base / "coworker.db")) == SCHEMA_VERSION


# -- it cannot be forgotten by the next store -------------------------------------
def test_every_module_that_opens_a_database_guards_it():
    """A new store that forgets `ensure_version` silently loses the protection, and
    nothing would notice until a downgrade had already written to a newer file. Same
    shape of enforcement as tests/test_sandbox_runner.py's import rules.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "coworker"
    unguarded = []
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "sqlite3.connect" not in source:
            continue
        tree = ast.parse(source)
        opens = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "connect"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "sqlite3"
            for n in ast.walk(tree)
        )
        guards = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ensure_version"
            for n in ast.walk(tree)
        )
        if opens and not guards:
            unguarded.append(path.relative_to(package.parent).as_posix())

    assert unguarded == [], (
        f"these modules open a SQLite database without calling coworker.dbschema.ensure_version on it: {unguarded}"
    )
