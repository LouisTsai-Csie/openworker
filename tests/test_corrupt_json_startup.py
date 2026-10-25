"""A corrupt JSON state file must never stop the server starting (#204).

`mentions.py` and `ParkedStore` were fixed for this in #709; the other JSON stores named
in #204 were not, and six of them are constructed eagerly by `SessionManager.__init__` —
so one truncated file meant `openworker-server` died on every start until the user found
and deleted it by hand.

This is the read-side half of D1. The write side (atomic, fsynced writes, so a crash stops
*producing* torn files) landed first; it does nothing for the torn files already sitting
in users' state directories, which is what these tests are about.

Two properties, both load-bearing:

* **start, don't raise** — an unreadable file leaves the store at its constructed default;
* **all or nothing** — a file whose fifth record is bad does not leave the first four
  loaded. Every loader builds into locals and assigns only on success, because a
  half-populated store is harder to reason about than an empty one and silently drops
  whatever came after the bad record.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from coworker.connections import PersonaConnectionStore, SessionConnectionStore
from coworker.inbox import InboxStore
from coworker.inbox_routing import DEFAULT_INBOX, InboxRouting
from coworker.overrides import RiskOverrideStore
from coworker.personas.registry import PersonaRegistry
from coworker.subscriptions import SubscriptionStore
from coworker.unrouted import UnroutedStore

# Every file shape a corrupt store can present. The truncation is what an interrupted
# pre-atomic write actually left behind; the rest are files that parse but are not what
# the loader expects (#709 covered these for mentions.py only).
CORRUPT_BODIES = pytest.mark.parametrize(
    "body",
    [
        pytest.param('{"items": [\n  {"id": "abc', id="truncated"),
        pytest.param("", id="empty"),
        pytest.param("[]", id="json-list"),
        pytest.param("null", id="json-null"),
        pytest.param('"hello"', id="json-string"),
        pytest.param("{]", id="not-json"),
    ],
)

# (class, the key its loader reads, how to ask the store what it holds)
STORES = [
    pytest.param(InboxStore, "items", lambda s: s.list(), id="inbox"),
    pytest.param(SubscriptionStore, "subscriptions", lambda s: s.all(), id="subscriptions"),
    pytest.param(UnroutedStore, "items", lambda s: s.list(), id="unrouted"),
    pytest.param(PersonaConnectionStore, "personas", lambda s: s.get("p1"), id="persona-connections"),
    pytest.param(SessionConnectionStore, "sessions", lambda s: s.get("s1"), id="session-connections"),
]


@pytest.mark.parametrize("store_cls,key,read", STORES)
@CORRUPT_BODIES
def test_a_corrupt_file_starts_empty_instead_of_raising(tmp_path, store_cls, key, read, body):
    path = tmp_path / "store.json"
    path.write_text(body, encoding="utf-8")

    store = store_cls(path)  # must not raise

    assert not read(store)


@pytest.mark.parametrize("store_cls,key,read", STORES)
def test_recovery_the_next_write_replaces_the_unreadable_file(tmp_path, store_cls, key, read):
    path = tmp_path / "store.json"
    path.write_text('{"items": [\n  {"id": "abc', encoding="utf-8")

    store_cls(path)._save()

    assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


@CORRUPT_BODIES
def test_the_persona_registry_falls_back_to_its_builtin_defaults(tmp_path, body):
    path = tmp_path / "personas.json"
    path.write_text(body, encoding="utf-8")

    registry = PersonaRegistry(state_path=path)

    assert registry.default_id()


@CORRUPT_BODIES
def test_inbox_routing_keeps_its_default_binding(tmp_path, body):
    """Its constructor seeds a default binding before loading, so "start empty" here means
    "keep the default", not "lose it"."""
    path = tmp_path / "inbox_routing.json"
    path.write_text(body, encoding="utf-8")

    routing = InboxRouting(path)

    assert DEFAULT_INBOX in routing._bindings


# -- all or nothing ---------------------------------------------------------------
def test_one_bad_record_does_not_leave_the_earlier_ones_loaded(tmp_path):
    path = tmp_path / "unrouted.json"
    path.write_text(
        json.dumps(
            {
                "items": [
                    {"source": "slack", "sender": "a", "text": "one", "reason": "r"},
                    {"source": "slack", "sender": "b", "text": "two", "reason": "r"},
                    {"nonsense": True},  # wrong shape: UnroutedItem(**raw) raises TypeError
                ]
            }
        ),
        encoding="utf-8",
    )

    store = UnroutedStore(path)

    assert store.list() == [], "a partial load silently drops everything after the bad record"


def test_inbox_does_not_half_load_either(tmp_path):
    path = tmp_path / "inbox.json"
    path.write_text(
        json.dumps({"items": [{"id": "ok", "session_id": "s", "kind": "approval"}, {"nope": 1}]}),
        encoding="utf-8",
    )

    assert InboxStore(path).list() == []


# -- the permissions store fails in the safe direction ----------------------------
@CORRUPT_BODIES
def test_a_corrupt_override_file_grants_nothing(tmp_path, body):
    """Starting with no overrides means every tool falls back to its default risk class,
    so the user is asked MORE often. Failing the other way would silently widen what runs
    without an approval card."""
    path = tmp_path / "risk_overrides.json"
    path.write_text(body, encoding="utf-8")

    store = RiskOverrideStore(path)

    assert store._rules == []
    assert store._trust == []


def test_a_corrupt_override_file_is_left_on_disk_for_the_user_to_fix(tmp_path):
    """A hand-editing typo must stay recoverable: loading does not rewrite the file."""
    path = tmp_path / "risk_overrides.json"
    path.write_text('{"rules": [{"pattern": "x", "risk": ', encoding="utf-8")

    RiskOverrideStore(path)

    assert path.read_text(encoding="utf-8") == '{"rules": [{"pattern": "x", "risk": '


def test_one_malformed_rule_still_only_skips_that_rule(tmp_path):
    """The distinction the loader draws: a file that is not an override file is refused
    whole, but an override file with one bad line keeps the good lines."""
    path = tmp_path / "risk_overrides.json"
    path.write_text(
        json.dumps({"rules": [{"pattern": "good_*", "risk": "read"}, {"pattern": "bad_*"}]}),
        encoding="utf-8",
    )

    store = RiskOverrideStore(path)

    assert [r.pattern for r in store._rules] == ["good_*"]


# -- the real failure: the server used to die on start ----------------------------
@pytest.mark.parametrize(
    "filename",
    [
        "inbox.json",
        "inbox_routing.json",
        "subscriptions.json",
        "persona_connections.json",
        "session_connections.json",
        "session_connector_extensions.json",
        "unrouted.json",
        "personas.json",
    ],
)
def test_the_manager_still_starts_with_any_one_file_corrupt(tmp_path, filename):
    from coworker.server.manager import SessionManager

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / filename).write_text('{"items": [\n  {"id": "abc', encoding="utf-8")

    manager = SessionManager(data_dir=data, workspace=tmp_path / "repo")  # must not raise

    assert manager is not None


# -- it cannot be forgotten by the next store -------------------------------------
def test_no_store_loads_json_without_a_guard():
    """The ratchet for #204: 7 of 16 loaders were unguarded when this landed, and the
    next one added would have been the eighth. Same shape of enforcement as
    tests/test_sandbox_runner.py's import rules.
    """
    # Not a state file: `load_credentials` parses a service-account JSON the USER
    # supplied. Swallowing a malformed one would silently fall back to Application
    # Default Credentials — a different identity than the one they configured — so
    # raising is the correct behaviour there, not a missing guard.
    not_state_files = {"coworker/providers/vertex_provider.py:load_credentials"}

    package = Path(__file__).resolve().parents[1] / "coworker"
    unguarded = []
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith(("_load", "load")):
                continue
            loads = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "loads"
                and getattr(n.func.value, "id", "") == "json"
            ]
            if not loads:
                continue
            guarded = {id(n) for t in ast.walk(fn) if isinstance(t, ast.Try) and t.handlers for n in ast.walk(t)}
            rel = path.relative_to(package.parent).as_posix()
            if f"{rel}:{fn.name}" in not_state_files:
                continue
            if not all(id(n) in guarded for n in loads):
                unguarded.append(f"{rel}:{fn.lineno} {fn.name}()")

    assert unguarded == [], (
        "these loaders let a corrupt state file raise out of the constructor, which stops "
        f"the server starting (#204): {unguarded}"
    )
