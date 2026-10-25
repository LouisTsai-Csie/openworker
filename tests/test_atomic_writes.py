"""The write-side durability guarantee every JSON store in the tree now depends on.

`path.write_text(...)` truncates at open() and then streams, so a crash in between leaves
a half-written settings/secrets file that the next start reads as corrupt. ARCHITECTURE.md
§7 D1. `coworker.atomicio` replaces that with write-temp → fsync → rename → fsync-dir.

The ordering tests are the point of this file. Any of those four steps can be present and
still be useless if it happens at the wrong moment: an `fsync` after the `os.replace` does
not stop a power cut from exposing a complete-looking file full of zeros, which is exactly
the state the two pre-existing "atomic" writes in this codebase could reach.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from coworker import atomicio
from coworker.atomicio import atomic_path, atomic_write_json, atomic_write_text

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits / directory fsync")


def _leftovers(folder: Path, *keep: str) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name not in keep)


# -- the happy path ---------------------------------------------------------------
def test_json_round_trips_and_creates_missing_parents(tmp_path):
    target = tmp_path / "deep" / "nested" / "settings.json"
    atomic_write_json(target, {"enabled": True, "rules": [1, 2]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"enabled": True, "rules": [1, 2]}


def test_default_indent_matches_what_the_stores_already_wrote(tmp_path):
    """These files are read by people during support; the sweep must not reflow them."""
    target = tmp_path / "a.json"
    atomic_write_json(target, {"b": 1})
    assert target.read_text(encoding="utf-8") == '{\n  "b": 1\n}'
    atomic_write_json(target, {"b": 1}, indent=None)
    assert target.read_text(encoding="utf-8") == '{"b": 1}'


def test_no_temp_file_is_left_behind(tmp_path):
    atomic_write_json(tmp_path / "a.json", {"b": 1})
    assert _leftovers(tmp_path) == ["a.json"]


def test_overwrite_replaces_content_entirely(tmp_path):
    """A shorter second write must not leave the tail of the first one behind."""
    target = tmp_path / "a.json"
    atomic_write_json(target, {"k": "x" * 500})
    atomic_write_json(target, {"k": "short"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "short"}


def test_the_temp_lives_in_the_targets_own_directory(tmp_path):
    """A temp anywhere else makes the final rename cross-device, and os.replace raises."""
    target = tmp_path / "state" / "a.json"
    seen: list[Path] = []
    atomic_write_json(target, {"b": 1}, on_tempfile=seen.append)
    assert seen and seen[0].parent == target.parent


# -- the ordering that makes it durable -------------------------------------------
def test_content_is_fsynced_before_the_rename(tmp_path, monkeypatch):
    """The step both pre-existing atomic writes were missing.

    Without it the rename can reach the disk before the bytes do, and a power cut leaves a
    file that exists, has the right length, and is full of zeros.
    """
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    monkeypatch.setattr(atomicio.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(atomicio.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])

    atomic_write_json(tmp_path / "a.json", {"b": 1})

    assert "fsync" in calls and "replace" in calls
    assert calls.index("fsync") < calls.index("replace"), f"content fsync must precede the rename: {calls}"


@posix_only
def test_the_directory_is_fsynced_after_the_rename(tmp_path, monkeypatch):
    """os.replace only reorders a directory entry in the page cache; without this the
    rename itself is what a power cut loses, content fsync or not."""
    calls: list[str] = []
    real_replace = os.replace

    monkeypatch.setattr(atomicio.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    monkeypatch.setattr(atomicio, "fsync_directory", lambda d: calls.append("fsync_dir"))

    atomic_write_json(tmp_path / "a.json", {"b": 1})

    assert calls == ["replace", "fsync_dir"]


def test_fsync_directory_survives_a_filesystem_that_refuses_it(tmp_path, monkeypatch):
    """Network mounts answer EINVAL here. The content is already safe, so this must not
    turn a successful write into a raised error."""

    def refuse(fd):
        raise OSError("EINVAL")

    monkeypatch.setattr(atomicio.os, "fsync", refuse)
    atomicio.fsync_directory(tmp_path)  # must not raise


# -- failure leaves the old file exactly as it was ---------------------------------
def test_an_unencodable_value_fails_before_anything_reaches_the_disk(tmp_path):
    target = tmp_path / "a.json"
    atomic_write_json(target, {"generation": 1})

    with pytest.raises(TypeError):
        atomic_write_json(target, {"generation": object()})

    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 1}
    assert _leftovers(tmp_path) == ["a.json"], "a failed serialisation must not create a temp at all"


def test_a_failed_rename_leaves_the_previous_file_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "a.json"
    atomic_write_json(target, {"generation": 1})

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(atomicio.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(target, {"generation": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 1}
    assert _leftovers(tmp_path) == ["a.json"], "temp must be cleaned up"


def test_a_failed_on_tempfile_hook_leaves_nothing_behind(tmp_path):
    def boom(_path):
        raise OSError("chmod failed")

    with pytest.raises(OSError):
        atomic_write_json(tmp_path / "a.json", {"b": 1}, on_tempfile=boom)

    assert _leftovers(tmp_path) == []


# -- the hook, and the predictable-temp-name hole it closes ------------------------
def test_on_tempfile_runs_before_any_content_is_written(tmp_path):
    """`coworker.secrets` hangs its 0600-before-the-first-byte guarantee on this."""
    sizes: list[int] = []
    atomic_write_json(tmp_path / "a.json", {"b": 1}, on_tempfile=lambda p: sizes.append(p.stat().st_size))
    assert sizes == [0]


@posix_only
def test_on_tempfile_can_restrict_the_mode_the_content_is_written_at(tmp_path):
    target = atomic_write_json(tmp_path / "a.json", {"b": 1}, on_tempfile=lambda p: p.chmod(0o600))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_hostile_preexisting_temp_name_cannot_redirect_the_write(tmp_path):
    """A fixed `<name>.tmp` is predictable, so a symlink planted there captures the write.
    mkstemp picks an unpredictable name and creates it O_EXCL."""
    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber")
    try:
        (tmp_path / "a.json.tmp").symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    atomic_write_json(tmp_path / "a.json", {"b": 1})
    assert victim.read_text() == "do not clobber"


# -- atomic_path, for producers that need a path rather than a handle --------------
def test_atomic_path_swaps_in_the_finished_file(tmp_path):
    target = tmp_path / "runner.pyz"
    with atomic_path(target) as tmp, zipfile.ZipFile(tmp, "w") as zf:
        zf.writestr("__main__.py", "print(1)")
    with zipfile.ZipFile(target) as built:
        assert built.read("__main__.py") == b"print(1)"
    assert _leftovers(tmp_path) == ["runner.pyz"]


def test_atomic_path_does_not_publish_a_failed_build(tmp_path):
    target = tmp_path / "runner.pyz"
    target.write_bytes(b"the previous good bundle")

    with pytest.raises(RuntimeError), atomic_path(target) as tmp:
        tmp.write_bytes(b"half a bundle")
        raise RuntimeError("build failed")

    assert target.read_bytes() == b"the previous good bundle"
    assert _leftovers(tmp_path) == ["runner.pyz"]


def test_atomic_write_text_is_what_json_is_built_on(tmp_path):
    target = atomic_write_text(tmp_path / "models.json", '{"a": 1}\n')
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'
