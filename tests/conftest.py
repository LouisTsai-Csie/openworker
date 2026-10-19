"""Shared pytest fixtures.

`fake_slack` boots the in-process FakeSlack harness on an ephemeral port and points the Slack
adapter at it via `SLACK_API_URL`, so the real `SlackAdapter` / `slack_bolt` stack runs
end-to-end with no network, tokens, or the Slack app console. See
`coworker.testing.fake_slack` and `platform/docs/FAKE-SLACK-SPEC.md`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from coworker.testing.fake_slack import FakeSlack


def assert_user_only(path: Path) -> None:
    """Assert `path` is readable by the current user alone, per the platform's own model.

    POSIX says this with mode bits. Windows has no such bits: `os.stat` derives `st_mode`
    from the read-only attribute only, so every writable file reports 0o666 and a
    `st_mode & 0o777 == 0o600` check can never pass there, not even for a file that
    `secrets._restrict_to_user` has correctly locked down with `icacls`. Assert the ACL
    instead, mirroring how the protection is applied in the first place.
    """
    if sys.platform == "win32":
        acl = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True
        ).stdout
        # `/inheritance:r` strips inherited ACEs; their absence is what proves the file
        # was restricted rather than left to inherit SYSTEM/Administrators from its parent.
        assert "(I)" not in acl, f"{path} still carries inherited ACEs:\n{acl}"
        for principal in ("NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"):
            assert principal not in acl, f"{path} grants {principal}:\n{acl}"
    else:
        assert (path.stat().st_mode & 0o777) == 0o600


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """EVERY test gets an isolated SecretStore/state dir. Without this, any test that builds
    a SessionManager reads the developer's real machine-global state — including their cloud
    sign-in, which made test session creation emit REAL telemetry to prod (found 2026-07-03
    as burst noise in the ocw-connect-telemetry-events table)."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "coworker-state"))
    # Universal scratch provisions a per-session dir for EVERY session — without this,
    # tests would mkdir under the developer's real ~/OpenWorker.
    monkeypatch.setenv("COWORKER_SCRATCH_BASE", str(tmp_path / "coworker-scratch"))
    monkeypatch.delenv("COWORKER_API_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _no_docker_host_network_probe(monkeypatch):
    """OpenShell's preflight asks, on a Mac, whether Docker Desktop's host networking is on,
    by starting a container. No unit test may do that; the check has its own tests."""
    from coworker.sandbox import setup_cmd
    from coworker.sandbox.providers import openshell

    monkeypatch.setattr(openshell, "host_network_problem", lambda: None)
    monkeypatch.setattr(setup_cmd, "_host_network_ok_at", 0.0)


@pytest_asyncio.fixture
async def fake_slack(monkeypatch):
    """A running FakeSlack control object; `SLACK_API_URL` is set to it for the test's duration."""
    fake = FakeSlack()
    await fake.start()
    monkeypatch.setenv("SLACK_API_URL", fake.api_url)
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture(autouse=True)
def _no_cached_ollama_facts():
    """Model facts are cached per Ollama server for a minute (providers/ollama_facts.py);
    a test must never see another test's fake server."""
    from coworker.providers import ollama_facts

    ollama_facts.forget_all()
    yield
    ollama_facts.forget_all()


GALLERY_OPUS, GALLERY_SONNET = "anthropic:claude-opus-4-8", "anthropic:claude-sonnet-4-8"


# -- Shared SessionManager fixtures -------------------------------------------------
#
# These live here rather than in a test module because several modules need them.
# A fixture imported across test modules (`from test_team_live_gaps import manager`)
# reads to pyflakes as an unused import shadowed by the test's own parameter — F401
# plus F811 — and `ruff check --fix` deletes it, with `fixture 'manager' not found`
# at collection time as the only symptom. conftest.py is how pytest shares a fixture.


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """A SessionManager with one `lead` session and a team of one worker (`sam`)."""
    import asyncio

    from coworker.server.manager import SessionManager
    from coworker.sessions import SessionRecord
    from coworker.teams.registry import TeamWorker

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "repo").mkdir()
    m = SessionManager(data_dir=tmp_path / "data", workspace=tmp_path / "repo")
    m.session_store.save(
        SessionRecord(
            session_id="lead",
            workspace=m.default_workspace,
            model="sample",
            mode="interactive",
            agent="swe-lead",
        )
    )
    m.teams.create(
        space=m._board_space("lead"),
        lead_session="lead",
        lead_actor="lead",
        workers=[TeamWorker(actor="sam", persona="swe-worker", session_id="worker")],
    )
    yield m
    asyncio.run(m.aclose())


@pytest.fixture
def gallery_manager(tmp_path, monkeypatch):
    """A SessionManager on a fake two-model machine, with github/linear secrets set.

    The card-gallery contract tests and the proposal-contract tests both build their
    payloads from this one, so the fixture has to outlive either module.
    """
    from coworker.server.manager import SessionManager
    from coworker.sessions import SessionRecord

    opus, sonnet = GALLERY_OPUS, GALLERY_SONNET
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "repo"
    ws.mkdir()
    m = SessionManager(data_dir=tmp_path / "data", workspace=str(ws))
    m.secrets.put("github:default", {"token": "ghp_test", "enabled": True})
    m.secrets.put("linear:default", {"token": "lin_test", "enabled": True})
    # A machine that can run two models, so the payload carries real model information.
    monkeypatch.setattr(m, "model_selectable", lambda model: model in (opus, sonnet))
    monkeypatch.setattr(m, "_curated_models", lambda: [opus, sonnet])
    m.session_store.save(
        SessionRecord(session_id="lead-sid", workspace=m.default_workspace, model=sonnet, mode="auto-approve", messages=[], agent="swe-lead")
    )
    yield m
