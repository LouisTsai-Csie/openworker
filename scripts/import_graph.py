#!/usr/bin/env python3
"""Measure coworker's import graph, and check it against the layering in ARCHITECTURE.md.

The layering is data, not prose: `LAYERS` below is the single definition, and every
number this script prints is derived from the source tree, so the architecture doc can
never quietly drift from the code.

What it reports:

  * module-level vs late (inside a function) import edges;
  * import-time cycles -- the ones that can actually fail at startup -- separately from
    cycles that only exist once late imports are counted;
  * for each late import, whether hoisting it to module level would CREATE a cycle. That
    is the only honest test of "this import is late on purpose", and it is the number
    that decides whether ruff's E402 can be switched back on;
  * imports that point from a lower layer up to a higher one, which is the one rule the
    layering exists to enforce.

Usage:
    python scripts/import_graph.py              # summary
    python scripts/import_graph.py --verbose    # every offending edge and cycle member
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "coworker"

# --- The layering ------------------------------------------------------------------
#
# A module may import from its own layer or any layer below it, never from above.
# Keys are matched against a module's first path segment after `coworker.` (so
# "providers" covers every `coworker.providers.*`), falling back to the full name for
# root-level modules.
LAYERS: dict[str, int] = {
    # L0 foundation -- no coworker dependencies beyond each other.
    "config": 0,
    "secrets": 0,
    "atomicio": 0,
    "basedir": 0,
    "clock": 0,
    "events": 0,
    "statelock": 0,
    "attachments": 0,
    "pdf_support": 0,
    "environment": 0,
    "toolchain": 0,
    "runtime_context": 0,
    # L1 governance -- decides what is allowed; depends only on foundation.
    "permissions": 1,
    "risk": 1,
    "audit": 1,
    "provenance": 1,
    "readonly": 1,
    "overrides": 1,
    "workspace_trust": 1,
    "unattended": 1,
    # L2 domain -- the nouns the product is about.
    "conversations": 2,
    "sessions": 2,
    "session_facts": 2,
    "inbox": 2,
    "inbox_routing": 2,
    "projects": 2,
    "project": 2,
    "roots": 2,
    "mentions": 2,
    "interactions": 2,
    "selfwake": 2,
    "unrouted": 2,
    "connections": 2,
    "subscriptions": 2,
    "subscription_sync": 2,
    "compaction": 2,
    "toolresult": 2,
    "model_config": 2,
    "cloud": 2,
    "reviewer": 2,
    # L3 capabilities -- things the agent can do.
    "providers": 3,
    "tools": 3,
    "sandbox": 3,
    "connectors": 3,
    "mcp": 3,
    "memory": 3,
    "skills": 3,
    "personas": 3,
    "agents": 3,
    "teams": 3,
    "automation": 3,
    "remote": 3,
    "web": 3,
    # L4 engine -- the turn loop.
    "engine": 4,
    # L5 composition -- assembles an agent from the layers below.
    "agent": 5,
    "catalog": 5,
    # L6 surfaces -- how a human or a network reaches the thing.
    "server": 6,
    "headless": 6,
    "tui": 6,
    "cli": 6,
}
LAYER_NAMES = {
    0: "foundation",
    1: "governance",
    2: "domain",
    3: "capabilities",
    4: "engine",
    5: "composition",
    6: "surfaces",
}
# Not part of the layering: the package itself, and test doubles.
UNLAYERED = {"", "testing"}

# --- The ratchet -------------------------------------------------------------------
#
# Debt that exists today, grandfathered so this can run as a CI gate. It only ever
# shrinks: a new violation fails the build, and so does an entry here that has been
# fixed, which forces the list to be pruned instead of quietly outliving the problem.
# ARCHITECTURE.md §4 explains each one and what the fix is.
KNOWN_CYCLES: frozenset[tuple[str, ...]] = frozenset(
    {
        # Plugin loading: descriptors.py ends by importing experimental, which imports
        # ConnectorDescriptor back. Fix by inverting to a register_descriptor() call.
        ("coworker.connectors.descriptors", "coworker.connectors.experimental"),
    }
)
KNOWN_VIOLATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("coworker.agents.code", "coworker.catalog"),
        ("coworker.agents.cowork", "coworker.catalog"),
        ("coworker.audit", "coworker.connectors"),
        ("coworker.interactions", "coworker.tools.ask"),
        ("coworker.model_config", "coworker.providers.recommended"),
        ("coworker.tools.subagent", "coworker.engine"),
    }
)


def module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def layer_key(module: str) -> str:
    """The name `LAYERS` is keyed by: the first segment under `coworker.`."""
    tail = module[len("coworker") :].lstrip(".")
    return tail.split(".")[0] if tail else ""


def layer_of(module: str) -> int | None:
    return LAYERS.get(layer_key(module))


def resolve(node: ast.ImportFrom | ast.Import, origin: str) -> list[str]:
    """Every `coworker.*` module this statement depends on."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names if a.name.startswith("coworker")]
    base = node.module or ""
    if node.level:
        pkg = origin.split(".")
        # A module's own package is its name minus the final segment; `__init__`
        # modules are already their package.
        pkg = pkg[: len(pkg) - node.level]
        base = ".".join([*pkg, base]) if base else ".".join(pkg)
    if not base.startswith("coworker"):
        return []
    # `from coworker.x import y` may name a module or a symbol; keep both candidates
    # and let the caller drop the ones that are not modules.
    return [base] + [f"{base}.{a.name}" for a in node.names]


def build() -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    modules = {module_name(p) for p in PKG.rglob("*.py")}
    top: dict[str, set[str]] = defaultdict(set)
    late: dict[str, set[str]] = defaultdict(set)
    for path in sorted(PKG.rglob("*.py")):
        origin = module_name(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - a parse error is a real failure
            print(f"SKIP {path}: {exc}", file=sys.stderr)
            continue
        # Mark every node that sits inside a function: those imports are deferred.
        deferred: set[int] = set()
        for parent in ast.walk(tree):
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(parent):
                    deferred.add(id(child))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            targets = {t for t in resolve(node, origin) if t in modules and t != origin}
            (late if id(node) in deferred else top)[origin] |= targets
    return modules, dict(top), dict(late)


def sccs(nodes: set[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan. Returns only the components that are genuine cycles."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = 0

    def walk(v: str) -> None:
        nonlocal counter
        index[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on.add(v)
        for w in sorted(edges.get(v, ())):
            if w not in index:
                walk(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in edges.get(v, ()):
                out.append(sorted(comp))

    for n in sorted(nodes):
        if n not in index:
            walk(n)
    return out


def reaches(src: str, dst: str, edges: dict[str, set[str]]) -> bool:
    seen, stack = {src}, [src]
    while stack:
        cur = stack.pop()
        if cur == dst:
            return True
        for nxt in edges.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true", help="list every offending edge")
    args = ap.parse_args()

    modules, top, late = build()
    n_top = sum(len(v) for v in top.values())
    n_late = sum(len(v) for v in late.values())

    print(f"modules {len(modules)}   module-level edges {n_top}   late edges {n_late}\n")

    # -- cycles ---------------------------------------------------------------------
    import_time = sccs(modules, top)
    combined_edges = {m: top.get(m, set()) | late.get(m, set()) for m in modules}
    combined = sccs(modules, combined_edges)
    print(f"cycles (module-level only) : {len(import_time)} ({sum(len(c) for c in import_time)} modules)")
    print(f"cycles (incl. late imports): {len(combined)} ({sum(len(c) for c in combined)} modules)")
    for comp in import_time:
        print(f"  import-time cycle: {' <-> '.join(comp)}")
    if args.verbose:
        for comp in combined:
            if comp not in import_time:
                print(f"  late-only cycle ({len(comp)}): {', '.join(comp)}")

    # -- are the late imports load-bearing? ------------------------------------------
    breaking: list[tuple[str, str]] = []
    hoistable: list[tuple[str, str]] = []
    for src, dsts in late.items():
        for dst in dsts:
            # Hoisting src -> dst creates a cycle only if dst already reaches src.
            (breaking if reaches(dst, src, top) else hoistable).append((src, dst))
    print(
        f"\nlate imports: {n_late} total, {len(breaking)} would create a cycle if "
        f"hoisted, {len(hoistable)} can be hoisted"
    )
    worst = sorted(((len(v), k) for k, v in late.items()), reverse=True)[:8]
    print("  most late imports:")
    for count, mod in worst:
        need = sum(1 for s, _ in breaking if s == mod)
        print(f"    {count:3}  {mod:42} must stay late: {need}")

    # -- layering --------------------------------------------------------------------
    unassigned = sorted({m for m in modules if layer_of(m) is None and layer_key(m) not in UNLAYERED})
    counts: dict[int, int] = defaultdict(int)
    for m in modules:
        lv = layer_of(m)
        if lv is not None:
            counts[lv] += 1
    print("\nlayers:")
    for lv in sorted(LAYER_NAMES):
        print(f"  L{lv} {LAYER_NAMES[lv]:<13} {counts[lv]:>3}")
    print(f"  unlayered         {len(modules) - sum(counts.values()):>3}")
    if unassigned:
        print(f"  !! no layer assigned: {', '.join(unassigned)}")

    def violations(edges: dict[str, set[str]]) -> list[tuple[str, str, int, int]]:
        out = []
        for src, dsts in edges.items():
            a = layer_of(src)
            if a is None:
                continue
            for dst in dsts:
                b = layer_of(dst)
                if b is not None and b > a:
                    out.append((src, dst, a, b))
        return sorted(out)

    v_top = violations(top)
    v_late = [v for v in violations(late) if (v[0], v[1]) not in {(a, b) for a, b, _, _ in v_top}]
    print(f"\nlayer violations: {len(v_top)} module-level, {len(v_late)} late-only")
    for src, dst, a, b in v_top:
        print(f"  L{a} {src}  ->  L{b} {dst}")
    if v_late and args.verbose:
        print("  (late-only:)")
        for src, dst, a, b in v_late:
            print(f"  L{a} {src}  ->  L{b} {dst}")

    # -- the ratchet -----------------------------------------------------------------
    seen_cycles = {tuple(c) for c in import_time}
    seen_violations = {(src, dst) for src, dst, _, _ in v_top}
    new_cycles = seen_cycles - KNOWN_CYCLES
    new_violations = seen_violations - KNOWN_VIOLATIONS
    fixed_cycles = KNOWN_CYCLES - seen_cycles
    fixed_violations = KNOWN_VIOLATIONS - seen_violations

    print()
    failed = False
    for cycle in sorted(new_cycles):
        print(f"FAIL new import-time cycle: {' <-> '.join(cycle)}")
        failed = True
    for src, dst in sorted(new_violations):
        print(f"FAIL new layer violation: {src} -> {dst}")
        failed = True
    for cycle in sorted(fixed_cycles):
        print(f"FAIL {' <-> '.join(cycle)} is fixed — drop it from KNOWN_CYCLES")
        failed = True
    for src, dst in sorted(fixed_violations):
        print(f"FAIL {src} -> {dst} is fixed — drop it from KNOWN_VIOLATIONS")
        failed = True
    if failed:
        return 1
    print(f"ok — {len(KNOWN_CYCLES)} known cycle(s), {len(KNOWN_VIOLATIONS)} known violation(s), nothing new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
