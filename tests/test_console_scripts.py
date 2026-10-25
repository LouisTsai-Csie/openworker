"""The package puts one command on the path: `openworker`. The server, the connectors
tool, the acceptor and the team board are run as modules while they are not products."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_only_openworker_is_a_console_script() -> None:
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"] == {"openworker": "coworker.cli:main"}
