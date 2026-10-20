#!/usr/bin/env python3
"""Inventory the tests that reach past the public API, and say what each one would take.

90 of 218 test files call a private `_method`. That is the thing standing between this
codebase and any serious refactor: the safety net is nailed to the implementation, so
moving a method breaks tests that were never about that method. Phase 0.4 of the plan is
to size that work before scheduling it, which means data rather than an estimate.

Each private method a test calls lands in one of three buckets:

  A  a public method of the same class reaches it, directly or through other private
     methods. The test can go through that public entry point, usually with no change
     to what is asserted. Cheapest to convert.
  B  nothing public calls it. Either the behaviour is genuinely private and the test is
     pinning an internal detail, or — more often — the method is a unit of behaviour that
     deserves to be its own module with its own public surface. Needs a decision.
  C  a module-level private function, not a method. Usually a pure helper; making it
     public (or moving it to a `_internal` module that tests may import) is a rename.

Usage:
    python scripts/private_api_usage.py            # summary
    python scripts/private_api_usage.py --verbose  # every method with its call sites
    python scripts/private_api_usage.py --bucket A # just one bucket
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "coworker"
TESTS = ROOT / "tests"


def _is_private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _calls_in(node: ast.AST) -> set[str]:
    """Every `self._x(...)` / `obj._x(...)` name called under `node`."""
    out: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and _is_private(child.func.attr)
        ):
            out.add(child.func.attr)
    return out


def scan_package() -> tuple[dict[str, set[str]], dict[str, set[str]], set[str]]:
    """(private method -> defining classes, private method -> the public methods that
    can reach it, module-level private functions).

    Reachability is transitive. A private method called only by another private method
    is still reachable if something public calls THAT, and a test can then go through the
    public entry point. Counting only direct callers overstates how much is genuinely
    sealed off.
    """
    defined_by: dict[str, set[str]] = defaultdict(set)
    reaches: dict[str, set[str]] = defaultdict(set)
    module_funcs: set[str] = set()
    # class -> method -> the private methods it calls
    graphs: dict[str, dict[str, set[str]]] = defaultdict(dict)

    for path in sorted(PKG.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    graphs[node.name][item.name] = _calls_in(item)
                    if _is_private(item.name):
                        defined_by[item.name].add(node.name)
        for node in tree.body:  # module level only
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_private(node.name):
                module_funcs.add(node.name)

    for cls, methods in graphs.items():
        for entry, direct in methods.items():
            if _is_private(entry):
                continue
            seen: set[str] = set()
            stack = list(direct)
            while stack:
                name = stack.pop()
                if name in seen:
                    continue
                seen.add(name)
                stack.extend(methods.get(name, ()))
            for name in seen:
                if name in methods:  # only names this class actually defines
                    reaches[name].add(f"{cls}.{entry}")
    return dict(defined_by), dict(reaches), module_funcs


def scan_tests() -> dict[str, set[str]]:
    """private method -> the test files that call it."""
    used: dict[str, set[str]] = defaultdict(set)
    for path in sorted(TESTS.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for name in _calls_in(tree):
            used[name].add(path.relative_to(ROOT).as_posix())
    return dict(used)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--bucket", choices=["A", "B", "C"])
    args = ap.parse_args()

    defined_by, reaches, module_funcs = scan_package()
    used = scan_tests()

    rows = []
    for name, files in used.items():
        classes = defined_by.get(name, set())
        callers = reaches.get(name, set())
        if not classes and name in module_funcs:
            bucket = "C"
        elif callers:
            bucket = "A"
        elif classes:
            bucket = "B"
        else:
            continue  # defined outside coworker (a test's own helper)
        rows.append((bucket, name, sorted(classes), sorted(callers), sorted(files)))
    rows.sort(key=lambda r: (r[0], -len(r[4]), r[1]))

    touched = sorted({f for _, _, _, _, files in rows for f in files})
    total_tests = len(list(TESTS.rglob("test_*.py")))
    print(f"{len(rows)} private names reached from {len(touched)} of {total_tests} test files\n")

    for bucket, title in [
        ("A", "reachable through an existing public method — convert the call"),
        ("B", "no public entry point reaches it — needs a decision"),
        ("C", "module-level private function — a rename, or a documented test seam"),
    ]:
        group = [r for r in rows if r[0] == bucket]
        files = sorted({f for r in group for f in r[4]})
        print(f"  {bucket}  {len(group):>3} names, {len(files):>3} test files  — {title}")

    print()
    for bucket, name, classes, callers, files in rows:
        if args.bucket and bucket != args.bucket:
            continue
        if not args.verbose and len(files) < 3:
            continue
        where = ", ".join(classes) if classes else "module-level"
        print(f"{bucket}  {name:<34} {where:<28} {len(files)} file(s)")
        if args.verbose:
            if callers:
                print(f"      reachable via: {', '.join(callers[:4])}")
            for f in files:
                print(f"      {f}")
    if not args.verbose:
        print("\n(names used by fewer than 3 test files are hidden; --verbose shows all)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
