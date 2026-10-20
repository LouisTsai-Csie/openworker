"""The HTTP surface is a contract: this pins every route's shape so a refactor cannot
quietly change it.

`create_app()` is 3,348 lines registering 257 routes in one function, and splitting it
into routers is the point of the next piece of work. The risk in that move is not that it
breaks loudly — it is that one route silently changes path, loses a query parameter, or
goes from required to optional, and nothing notices until a client 422s in production.
The GUI mirrors 217 of these paths by hand, so there is no type checker watching either.

What is pinned, per route: the methods, the path, the handler name, and every path, query
and body parameter with its type and default. That is the whole of what a caller can see
without executing the route.

To re-record after an INTENTIONAL change:

    UPDATE_ROUTE_CONTRACT=1 pytest tests/test_route_contract.py

and read the diff in the commit. An unexplained line in that diff is a bug.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute, APIWebSocketRoute

from coworker.server import SessionManager, create_app

CONTRACT = Path(__file__).parent / "contracts" / "http_routes.json"


def _field(field: Any) -> list[str]:
    """One parameter as [name, type, default]; `required` where there is no default."""
    annotation = getattr(field.field_info, "annotation", None)
    name = getattr(annotation, "__name__", None) or str(annotation)
    default = field.default
    # Pydantic's sentinel for "no default" has an unstable repr; say what it means.
    shown = "required" if type(default).__name__ == "PydanticUndefinedType" else repr(default)
    return [field.name, name, shown]


def _snapshot(app: Any) -> dict[str, Any]:
    routes = []
    sockets = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            dependant = route.dependant
            routes.append(
                {
                    "methods": sorted(route.methods),
                    "path": route.path,
                    "name": route.name,
                    "path_params": [_field(f) for f in dependant.path_params],
                    "query_params": [_field(f) for f in dependant.query_params],
                    "body_params": [_field(f) for f in dependant.body_params],
                }
            )
        elif isinstance(route, APIWebSocketRoute):
            sockets.append({"path": route.path, "name": route.name})
    routes.sort(key=lambda r: (r["path"], r["methods"]))
    sockets.sort(key=lambda r: r["path"])
    return {"routes": routes, "websockets": sockets}


def _describe(route: dict[str, Any]) -> str:
    return f"{','.join(route['methods'])} {route['path']}"


def test_the_http_surface_matches_the_recorded_contract(tmp_path):
    live = _snapshot(create_app(SessionManager(workspace=tmp_path)))

    if os.environ.get("UPDATE_ROUTE_CONTRACT"):
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
        return

    assert CONTRACT.exists(), (
        f"{CONTRACT} is missing; record it with UPDATE_ROUTE_CONTRACT=1"
    )
    recorded = json.loads(CONTRACT.read_text(encoding="utf-8"))

    was = {_describe(r): r for r in recorded["routes"]}
    now = {_describe(r): r for r in live["routes"]}

    gone = sorted(set(was) - set(now))
    added = sorted(set(now) - set(was))
    changed = [
        (key, was[key], now[key]) for key in sorted(set(was) & set(now)) if was[key] != now[key]
    ]

    problems: list[str] = []
    problems += [f"route removed: {key}" for key in gone]
    problems += [f"route added:   {key}" for key in added]
    for key, before, after in changed:
        for part in ("name", "path_params", "query_params", "body_params"):
            if before[part] != after[part]:
                problems.append(f"{key}: {part} was {before[part]} now {after[part]}")

    if recorded["websockets"] != live["websockets"]:
        problems.append(
            f"websockets were {recorded['websockets']} now {live['websockets']}"
        )

    assert not problems, (
        f"the HTTP surface changed in {len(problems)} way(s):\n  "
        + "\n  ".join(problems)
        + "\n\nIf every line above is intended, re-record with "
        "UPDATE_ROUTE_CONTRACT=1 pytest tests/test_route_contract.py"
    )


def test_every_route_is_reachable_by_a_unique_method_and_path(tmp_path):
    """Two handlers on the same method+path means one of them can never run."""
    live = _snapshot(create_app(SessionManager(workspace=tmp_path)))
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for route in live["routes"]:
        for method in route["methods"]:
            key = (method, route["path"])
            if key in seen:
                clashes.append(f"{method} {route['path']}: {seen[key]} and {route['name']}")
            seen[key] = route["name"]
    assert not clashes, "unreachable handlers:\n  " + "\n  ".join(clashes)
