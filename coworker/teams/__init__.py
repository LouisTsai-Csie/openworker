"""Agent teams substrate. Two append-only stores, one record discipline:
the board log (space-scoped — a board lives and dies with its team) and the
journal store (case-keyed — knowledge that outlives boards and teams)."""

from coworker.teams.journal import JournalStore
from coworker.teams.model import (
    Actor,
    AuthorityError,
    BoardError,
    BoardNotFoundError,
    ChainError,
    ItemState,
    Role,
)
from coworker.teams.store import TeamStore
from coworker.teams.tools import board_tools, journal_tools

__all__ = [
    "Actor",
    "AuthorityError",
    "BoardError",
    "BoardNotFoundError",
    "ChainError",
    "ItemState",
    "JournalStore",
    "Role",
    "TeamStore",
    "board_tools",
    "journal_tools",
]

# BoardDialect / LocalDialect / RemoteDialect live in .dialect, BoardTokens in
# .tokens — imported directly by their consumers (CLI, MCP server, `/v1/board`)
# to keep this package root light for the common in-app path.
