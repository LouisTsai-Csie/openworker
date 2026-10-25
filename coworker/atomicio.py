"""Atomic file writes — a write either lands whole or does not land at all.

Everything the product remembers that is not in SQLite is a JSON file under the state
directory: settings, secrets, personas, skills, inbox, team boards. They were written with
`path.write_text(json.dumps(...))`, which truncates the real file at `open()` and then
streams bytes into it. A crash, an OOM kill, a full disk or a power cut between those two
moments leaves a zero-length or half-written file, and the next start reads it back as
corrupt — which, for most of these stores, means "start empty". That is ARCHITECTURE.md
§7 D1, and this module is its fix.

The recipe, in the order the steps have to happen:

1. create a temp file **in the target's own directory**, so the rename at the end is
   within one filesystem and therefore atomic (a cross-device `os.replace` raises);
2. write the bytes, `flush()`, and `os.fsync()` the file — without this the rename can
   reach the disk before the data does, and a power cut leaves a complete-looking file
   full of zeros;
3. `os.replace()` over the target, which POSIX guarantees is atomic: a concurrent reader
   sees either the whole old file or the whole new one, never a mix, and never nothing;
4. `fsync` the *directory*, so the rename itself survives the power cut that step 2 just
   protected the content from.

Step 4 is the one that is almost always missing, and it is why the two writes the debt
register already called atomic were not durable: `os.replace` only reorders a directory
entry in the page cache.

`tempfile.mkstemp` rather than a fixed `<name>.tmp`: the fixed name is predictable, so a
local attacker can pre-create it as a symlink and redirect the write (this is the same
hole `secrets.py` closed for the secrets file in #143 — see `on_tempfile` for how that
module keeps its 0600-before-first-byte guarantee on top of this).

This is L0: it imports nothing from `coworker`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_IS_WINDOWS = sys.platform == "win32"

# Called with the still-empty temp file, before a single byte of content goes in. The one
# place to apply permissions that must never be missing from the file while it holds its
# content (see `coworker.secrets`).
OnTempFile = Callable[[Path], None]


def _discard(tmp: Path) -> None:
    """Remove a temp file we are abandoning. Never masks the error that caused it."""
    try:
        tmp.unlink()
    except OSError:
        pass


def fsync_directory(directory: str | os.PathLike[str]) -> None:
    """Flush a directory entry so a rename into it survives a power cut.

    Best-effort by design, and for two different reasons. Windows has no directory handle
    to sync — `os.open` on a directory fails there — and the durability of a rename is the
    filesystem's business instead. On POSIX, some filesystems and most network mounts
    answer `fsync` on a directory with EINVAL. In both cases the file's own `fsync` has
    already happened, so the content is safe either way; only the rename's durability is
    in question, and there is nothing further this process can do about it.
    """
    if _IS_WINDOWS:
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_closed_file(path: Path) -> None:
    """fsync a file the caller has already closed (the `atomic_path` case)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def atomic_path(
    target: str | os.PathLike[str],
    *,
    on_tempfile: OnTempFile | None = None,
) -> Iterator[Path]:
    """Yield a temp path to build `target` at; swap it in atomically on a clean exit.

    For producers that need a *path* rather than a handle — a `zipfile.ZipFile`, a
    subprocess writing its own output. The caller must have closed whatever it opened by
    the time the block ends. On any exception the temp is removed and `target` is left
    exactly as it was.
    """
    target = Path(target).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    os.close(fd)  # the caller opens it by path; mkstemp was for the safe, exclusive name
    tmp = Path(name)
    try:
        if on_tempfile is not None:
            on_tempfile(tmp)
        yield tmp
        _fsync_closed_file(tmp)
        os.replace(tmp, target)
    except BaseException:
        _discard(tmp)
        raise
    fsync_directory(target.parent)


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    on_tempfile: OnTempFile | None = None,
) -> Path:
    """Replace `path` with `text`, atomically and durably. Returns the path written.

    Missing parent directories are created. Raises whatever the write raises — callers for
    whom persistence is best-effort catch `OSError` themselves, because "the in-memory copy
    stays authoritative" is their policy to hold, not this module's.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(name)

    try:
        if on_tempfile is not None:
            on_tempfile(tmp)
    except BaseException:
        os.close(fd)  # not yet owned by a file object, so close it by hand
        _discard(tmp)
        raise

    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        _discard(tmp)
        raise

    fsync_directory(target.parent)
    return target


def atomic_write_json(
    path: str | os.PathLike[str],
    data: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    default: Callable[[Any], Any] | None = None,
    on_tempfile: OnTempFile | None = None,
) -> Path:
    """Serialise `data` as JSON and write it over `path` atomically.

    `indent=2` because that is what nearly every store in the tree already wrote, and these
    files get read by people during support. The serialisation happens *before* the temp
    file is created, so a value JSON cannot encode fails without leaving anything on disk.
    """
    text = json.dumps(data, indent=indent, sort_keys=sort_keys, default=default)
    return atomic_write_text(path, text, on_tempfile=on_tempfile)
