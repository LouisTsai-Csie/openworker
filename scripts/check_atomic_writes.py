"""Every JSON file write in `coworker/` goes through `coworker.atomicio`.

ARCHITECTURE.md §7 D1 was "53 modules write JSON, 2 writes atomic". The sweep that closed
it is worth nothing if the next store to be added writes `path.write_text(json.dumps(...))`
again — that spelling truncates the real file at `open()` and then streams into it, so a
crash in between leaves settings, secrets or a team board half-written.

This script is the gate that stops it coming back. It walks the AST rather than grepping,
so it sees the pattern however it is spelled across lines, and it ignores `json.dumps` that
goes anywhere other than a file (HTTP bodies, log lines, hashes — the large majority).

What counts as a JSON file write:

    path.write_text(json.dumps(...))      # whole-file replace  -> must be atomicio
    fh.write(json.dumps(...))             # a handle            -> append, or atomicio
    json.dump(obj, fh)                    # same

Appends are the allowed exception and are listed in `KNOWN_APPENDS` below. A `.jsonl` log
that is only ever appended to cannot be torn in the way a whole-file rewrite can: the worst
a power cut does is truncate the final line, which every reader here already tolerates, and
making each append fsync would turn a per-message write into a per-message disk flush.

Like the other gates in this repo the list only ever shrinks: a new write fails the build,
and so does an entry that no longer matches anything, which forces the list to be pruned
rather than quietly outlive the code it describes.

Usage:
    python scripts/check_atomic_writes.py              # names only what is wrong
    python scripts/check_atomic_writes.py --verbose    # every site it found
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "coworker"

# --- The exceptions ----------------------------------------------------------------
#
# (module, the name of the handle written to). Each is an append to a line-delimited log,
# never a whole-file rewrite.
KNOWN_APPENDS: frozenset[tuple[str, str]] = frozenset(
    {
        # Conversation transcripts, one line per message — the append path of save().
        ("coworker/conversations.py", "f"),
        # Headless run artifacts: the event stream and the per-model-call log.
        ("coworker/headless/runner.py", "events_fh"),
        ("coworker/headless/runner.py", "fh"),
        # The remote audit log. Appended per event, read back line by line.
        ("coworker/remote/audit.py", "fh"),
    }
)


def _is_json_dumps(node: ast.AST) -> bool:
    """True if evaluating `node` involves a `json.dumps(...)` / `json.dump(...)` call."""
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr in ("dumps", "dump")
        and isinstance(sub.func.value, ast.Name)
        and sub.func.value.id == "json"
        for sub in ast.walk(node)
    )


def _target(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        # An exotic target still has to be reported; its name is only for the message.
        return "<?>"


def json_writes(path: Path) -> list[tuple[int, str, str]]:
    """(line, kind, target) for every JSON-to-a-file write in one module."""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr, value = node.func.attr, node.func.value
        if attr == "write_text" and node.args and _is_json_dumps(node.args[0]):
            found.append((node.lineno, "write_text", _target(value)))
        elif attr == "write" and node.args and _is_json_dumps(node.args[0]):
            found.append((node.lineno, "write", _target(value)))
        elif attr == "dump" and isinstance(value, ast.Name) and value.id == "json":
            found.append((node.lineno, "json.dump", _target(node.args[1]) if len(node.args) > 1 else "<?>"))
    return sorted(found)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true", help="list every site, not just the bad ones")
    args = ap.parse_args()

    offenders: list[str] = []
    matched: set[tuple[str, str]] = set()
    total = allowed = 0

    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for line, kind, target in json_writes(path):
            total += 1
            key = (rel, target)
            if kind != "write_text" and key in KNOWN_APPENDS:
                matched.add(key)
                allowed += 1
                if args.verbose:
                    print(f"  append (allowed)  {rel}:{line}  {target}.write(json.dumps(...))")
                continue
            offenders.append(f"  {rel}:{line}  {target}.{kind}(json.dumps(...))")

    stale = sorted(KNOWN_APPENDS - matched)

    print(f"{total} JSON file-write site(s) in coworker/: {allowed} allowed append(s), {len(offenders)} direct")

    if offenders:
        print("\nThese write JSON straight to a file. Use coworker.atomicio instead:")
        print("  atomic_write_json(path, data)      whole-file replace, fsynced")
        print("  atomic_write_text(path, text)      when the text is not one JSON document")
        print("  atomic_path(target)                when a producer needs a path to build at")
        print("\n" + "\n".join(sorted(offenders)))
    if stale:
        print("\nKNOWN_APPENDS entries that no longer match anything — delete them:")
        for rel, target in stale:
            print(f"  {rel}  ({target})")

    if offenders or stale:
        return 1
    print("ok — every JSON write is atomic, every allowed append is still there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
