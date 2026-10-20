"""Write a file so a crash cannot leave it half-written.

Fifty-three modules in this package persist JSON — settings, personas, skills, team
boards, machine records. Before this module, two of those writes were atomic. The rest
were `json.dump(obj, open(path, "w"))`, which truncates the file first: lose power, lose
the process, lose the disk mid-write, and what is left on disk is a prefix of the new
content. On the next start that file does not parse, and whatever it held is gone.

The fix is the usual one, and it has three parts, all of which matter:

  1. write to a temp file in the SAME directory, so the rename below cannot cross a
     filesystem boundary and degrade into a copy;
  2. `fsync` the temp file before renaming, or the rename can reach the disk while the
     content is still in the page cache — the file then exists, is the right size, and
     is full of zeros after a power cut;
  3. `fsync` the directory after renaming, so the rename itself is durable.

`atomic_write_private_text` adds the protections the secret store needs: the temp file is
created 0600 before a byte goes in, rather than at the umask default and chmod-ed after.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

_IS_WINDOWS = sys.platform == "win32"


def restrict_to_user(path: Path, *, is_dir: bool) -> None:
    """Restrict a path so only the current user can access it.

    POSIX expresses this with mode bits (0700 dir / 0600 file). Windows has no such bits —
    `os.chmod` there only toggles the read-only flag, so a 0600 chmod is a silent no-op and
    the file inherits broad ACLs (SYSTEM, Administrators, …). Use an ACL instead: strip
    inherited entries and grant the current user alone. Best-effort on Windows so a transient
    icacls failure never blocks saving a key."""
    if _IS_WINDOWS:
        user = os.environ.get("USERNAME")
        if not user:
            return
        domain = os.environ.get("USERDOMAIN")
        account = f"{domain}\\{user}" if domain else user
        # A directory grant MUST be inheritable — (OI) object-inherit for files, (CI)
        # container-inherit for subdirs — so everything created inside (the SQLite stores,
        # conversations, …) inherits the user's access. Without these flags, /inheritance:r
        # leaves the directory with a non-inheritable ACE and any child file ends up with an
        # empty DACL → sqlite3 "unable to open database file", crashing the server on launch.
        grant = f"{account}:(OI)(CI)F" if is_dir else f"{account}:F"
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    os.chmod(path, 0o700 if is_dir else 0o600)


def _sync_dir(directory: Path) -> None:
    """Make the rename itself durable. Not a thing on Windows, which has no directory fd."""
    if _IS_WINDOWS:
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(
    target: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    on_create: Callable[[Path], None] | None = None,
) -> Path:
    """Replace `target` with `content`, or leave it exactly as it was.

    `on_create` is handed the still-empty temp file, for a caller that must lock it down
    before any content exists on disk.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        if on_create is not None:
            on_create(tmp)
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
            fh.flush()
            # Before the rename, not after: a rename that lands while the content is
            # still in the page cache leaves a right-sized file full of zeros.
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _sync_dir(target.parent)
    return target


def atomic_write_private_text(target: Path, content: str) -> Path:
    """`atomic_write_text` with the temp file readable by this user alone.

    The temp file used to be created by `Path.write_text` and only chmod-ed afterwards, so
    the plaintext sat on disk at the umask default (0644 on a normal box) for the length of
    the write — readable by every local process and by anything backing the directory up.
    That is issue #143.

    `tempfile.mkstemp` creates with 0600 and O_EXCL before a byte is written, which also
    removes the fixed `<name>.tmp` filename. That name was predictable, so a local attacker
    could pre-create it as a symlink and have the write land wherever the link pointed.

    Windows gets no mode bits from mkstemp, so the ACL is applied to the still-empty file
    before the content goes in.
    """
    try:
        restrict_to_user(target.parent, is_dir=True)
    except OSError:
        pass
    return atomic_write_text(
        target, content, on_create=lambda tmp: restrict_to_user(tmp, is_dir=False)
    )


def atomic_write_json(target: Path, data: Any, *, indent: int | None = 2) -> Path:
    """The JSON form of `atomic_write_text`, with a trailing newline."""
    return atomic_write_text(target, json.dumps(data, indent=indent) + "\n")
