"""Crash-safety of ConversationStore's shrink rewrite (write-side atomicity).

`save()` appends new messages on the common path, but when a turn *reduces* the message
count (context compaction / summarization) it rewrites the whole ``.jsonl``. That rewrite
must be atomic: a crash partway through must not truncate or erase the existing history —
the most valuable data the app holds.

This is the write-side complement to read-side corrupt-line tolerance: prevent the
truncated file rather than cope with one after the fact.

The mechanism itself lives in `coworker.atomicio` (tests/test_atomic_writes.py); what is
checked here is that the shrink path actually goes through it.
"""

from __future__ import annotations

import os

import pytest

from coworker import atomicio
from coworker.conversations import ConversationStore
from coworker.sessions import SessionRecord


def _rec(sid: str, n: int) -> SessionRecord:
    return SessionRecord(
        session_id=sid,
        workspace="/w",
        model="m",
        mode="interactive",
        messages=[{"role": "user", "content": f"msg-{i}"} for i in range(n)],
    )


def test_shrink_rewrite_preserves_history_when_write_crashes(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path)
    sid = "sess1"

    # Persist a 5-message history via the append path.
    store.save(_rec(sid, 5))
    assert len(store.load(sid).messages) == 5

    # Force a crash partway through the shrink rewrite (5 -> 2): half the bytes reach the
    # temp file and then the write fails. A non-atomic in-place open(..., "w") truncates the
    # real file at open() and the crash then erases the history; the atomic write leaves the
    # original untouched because the rename never happens.
    real_fdopen = os.fdopen

    class _CrashingFile:
        """Writes half of what it is given, then dies."""

        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

        def write(self, s):
            self._fh.write(s[: len(s) // 2])
            raise OSError("simulated crash mid-write")

    monkeypatch.setattr(atomicio.os, "fdopen", lambda fd, *a, **kw: _CrashingFile(real_fdopen(fd, *a, **kw)))

    with pytest.raises(OSError):
        store.save(_rec(sid, 2))

    monkeypatch.undo()

    # The interrupted shrink must not have destroyed the existing history.
    assert len(store.load(sid).messages) == 5


def test_shrink_rewrite_persists_reduced_history(tmp_path):
    """The (non-crash) shrink path still rewrites the log to exactly the reduced set."""
    store = ConversationStore(tmp_path)
    sid = "sess2"

    store.save(_rec(sid, 5))
    assert len(store.load(sid).messages) == 5

    store.save(_rec(sid, 3))  # shrink 5 -> 3
    reloaded = store.load(sid)
    assert [m["content"] for m in reloaded.messages] == ["msg-0", "msg-1", "msg-2"]

    # No leftover temp file next to the conversation log. The temp name is unpredictable
    # (mkstemp), so this asserts on the suffix rather than on one expected name.
    assert [p.name for p in (tmp_path / "conversations").iterdir() if p.name.endswith(".tmp")] == []
