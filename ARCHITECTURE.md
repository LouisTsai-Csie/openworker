# Architecture

How OpenWorker is put together, which direction its dependencies are allowed to run, and
where the known debt is.

Every number here was measured against the tree, not estimated. `scripts/import_graph.py`
reproduces the import-graph figures in §3 and §4; the rest are one-line `grep`s named
beside the claim. If a number has drifted, the script is the source of truth, not this
file.

Measured 2026-10-20: `coworker/` is 231 modules / 77,814 lines, `tests/` 218 files /
56,708 lines, `surfaces/gui` 51,263 lines of TS/TSX.

---

## 1. Execution topology

OpenWorker runs as one Python process that owns all state, plus whichever surface the
person is using.

```
 surfaces                      process                         outside
 ─────────                     ───────                         ───────
 Tauri desktop ──┐
 browser GUI   ──┼── HTTP+WS ──▶ coworker.server ──┐
 CLI (openworker)│                                 ├──▶ model providers
 TUI             │                                 ├──▶ MCP servers
 headless run  ──┘                                 ├──▶ connectors (Slack, GitHub, …)
                                                   └──▶ sandboxed shells
```

A turn is driven by `coworker.engine.TurnEngine`: it asks a provider for the next
assistant turn, runs whatever tools that turn calls, and yields `Event`s as it goes. Every
surface consumes that same event stream — the server relays it over a WebSocket, the
headless runner prints it, the TUI draws it. There is no second turn loop anywhere.

State lives on disk under the state directory, not in memory: a restart rebuilds engines
from the session store.

## 2. Packages

| Package | Responsibility |
|---|---|
| `providers` | One adapter per model vendor, normalising to `AssistantTurn` / `StreamChunk`. |
| `tools` | The built-in tool surface (shell, files, git, search, web, …). |
| `sandbox` | Where a shell actually runs: seatbelt, OpenShell, Windows, plus the runner mounted into the sandbox. |
| `connectors` | Outbound/inbound integrations and the 136 SaaS tools built from them. |
| `mcp` | MCP client: stdio and streamable-http, OAuth, tool projection. |
| `memory`, `skills`, `personas` | What the agent knows, can learn, and is configured to be. |
| `agents`, `teams`, `automation` | Multi-agent composition, the team board, scheduled work. |
| `remote` | Remote homes: the acceptor, the joiner, the audit sink. |
| `web` | Fetch and search, behind an egress guard. |
| `server`, `headless`, `tui`, `cli` | The surfaces. |
| `testing` | In-process fakes (FakeSlack) used by tests; not part of the layering. |

## 3. The layering

Seven layers. **A module may import from its own layer or any layer below it, and never
from a layer above it.** That is the whole rule.

```
  L6  surfaces      server · headless · tui · cli                        12 modules
  L5  composition   agent · catalog                                       2
  L4  engine        engine                                                1
  L3  capabilities  providers · tools · sandbox · connectors · mcp ·
                    memory · skills · personas · agents · teams ·
                    automation · remote · web                           172
  L2  domain        conversations · sessions · inbox · projects ·
                    interactions · compaction · model_config · …         20
  L1  governance    permissions · risk · audit · provenance · …            8
  L0  foundation    config · secrets · basedir · clock · events · …       11
```

The layering is defined as data in `scripts/import_graph.py` (`LAYERS`), so it can be
checked rather than believed. `testing` and the `coworker` package module itself are
deliberately unlayered (5 modules).

Why this order: governance has to be reachable from everything that could be governed, so
it sits low. The engine sits *above* capabilities because it orchestrates them and must
not be imported by them. Surfaces sit on top because nothing in the product should care
whether a human is watching.

## 4. Measured conformance

Run `python scripts/import_graph.py` (`--verbose` for every edge). As of 2026-10-20:

```
modules 231   module-level edges 471   late edges 294

cycles (module-level only) : 1 (2 modules)
cycles (incl. late imports): 5 (23 modules)
  import-time cycle: coworker.connectors.descriptors <-> coworker.connectors.experimental

late imports: 294 total, 8 would create a cycle if hoisted, 286 can be hoisted

layer violations: 6 module-level, 11 late-only
```

**The dependency direction is healthy.** One import-time cycle in the whole package, and
it is a deliberate plugin-loading pattern: `descriptors.py` ends by importing
`experimental`, which imports `ConnectorDescriptor` back. It survives only because the
class is fully defined by the time the trailing import runs — which is exactly why it
should be inverted to a `register_descriptor()` call rather than left as a cycle.

**Late imports are mostly not load-bearing.** Only 8 of 294 would create a cycle if moved
to module level. The remaining 286 are hoistable. This matters because `pyproject.toml`
currently disables ruff's `E402` on the grounds that "deliberate late imports break import
cycles" — true for 8 of them, not for the other 286. Deferring an optional dependency
(boto3, playwright, slack_bolt, telegram) is a separate and valid reason, but it accounts
for only a handful.

The modules with the most late imports — `server.manager` (48), `server.app` (32),
`headless.runner` (21) — need **zero** of them to stay late.

### The six module-level violations

Each is a single import, and each has an obvious fix.

| From | To | Import | Fix |
|---|---|---|---|
| `audit` (L1) | `connectors` (L3) | `connector_for_tool` | Pass the connector name in at the call site, or move the tool→connector lookup down to L0. |
| `interactions` (L2) | `tools.ask` (L3) | `SKIP_SENTINEL`, `option_label` | These are protocol constants: move them down to L2 and let `tools.ask` import them. |
| `model_config` (L2) | `providers.recommended` (L3) | `recommendation_for` | `model_config` is really a providers concern; promote it to L3. |
| `agents.code` (L3) | `catalog` (L5) | `expand` | Inject the expanded catalog at construction; the agent registry is already building these. |
| `agents.cowork` (L3) | `catalog` (L5) | `expand` | As above. |
| `tools.subagent` (L3) | `engine` (L4) | `TurnEngine` | Inject an engine factory; keep the import under `TYPE_CHECKING`. |

Eleven more violations exist only via late imports. The one to fix first is
`remote.joiner → server.app` / `server.manager`: a capability reaching up into a surface
is precisely what the layering exists to prevent, and it is invisible today because the
import is deferred.

Of the four late-only cycles, three are contained inside a single package
(`connectors` ×2, `sandbox`). The fourth spans a package boundary —
`model_config` (L2) ↔ the `providers` package (L3) — and is the same violation listed
above, seen from the other side.

## 5. State on disk

Everything lives under the state directory (`coworker.secrets.state_dir()`, which is
really a `basedir` concern and sits in the wrong module today).

- **SQLite** — `coworker.db`, shared by five or more independent stores, each creating its
  own tables. 26 `CREATE TABLE` sites, 25 ad-hoc `ALTER TABLE` migrations, and
  `PRAGMA user_version` is never set. There is no schema version and no migration
  framework.
- **JSON files** — settings, secrets, personas, skills, team boards. 53 modules call
  `json.dump`; exactly two writes are atomic (`os.replace`, in `secrets.py` and
  `sandbox/bundle.py`).
- **JSONL** — conversation transcripts, appended per message.

## 6. Cross-cutting conventions

- **Errors inside a turn are values, not exceptions.** A tool that fails returns a result
  the model can read. This is why `except Exception` is broad in the runtime paths and why
  ruff's `BLE001` is disabled: one bad connector must not take the server down. Narrowing
  these is a per-site judgement, not a sweep.
- **Approval is a gate, not a callback.** `permissions` decides; the engine parks the turn
  and waits. Nothing below L1 decides whether an action is allowed.
- **Secrets never enter prompts.** They are read from the `SecretStore` at execution time.
- **The sandbox runner is standalone.** `coworker/sandbox/runner/` is repackaged as a
  zipapp named `owrunner` and mounted into images where `coworker` does not exist, so its
  intra-package imports must stay **relative** and it may import nothing but the standard
  library. `tests/test_sandbox_runner.py` enforces both.

## 7. Debt register

Ordered by what it costs to leave alone, not by size.

| # | Debt | Measured | Cost of leaving it |
|---|---|---|---|
| D1 | Non-atomic JSON writes | 53 modules write JSON, 2 writes atomic; `_atomic_private_write` (`secrets.py:99`) is correct but has no `fsync` before `os.replace` | **Data loss.** A crash or power cut mid-write leaves a truncated settings/secrets file. |
| D2 | `SessionManager` god object | 347 methods / 8,357 lines in one class; `__init__` wires 20+ subsystems | Nothing in the server can be tested or changed in isolation. |
| D3 | No typed API contract | 231 routes, **0** Pydantic models, bodies are raw `dict` | Front end mirrors 217 paths by hand; a shape change breaks it silently. |
| D4 | Domain failures return HTTP 200 | 229 `{"ok": False}` sites | Clients cannot distinguish "worked" from "refused" by status. |
| D5 | `create_app` god function | 231 routes / 3,348 lines | Same as D2, for the HTTP surface. |
| D6 | `make_integration_tools` god function | 136 tools / 4,505 lines across ~15 SaaS vendors | Any one vendor's change touches a 5,000-line file. |
| D7 | No schema versioning | 26 `CREATE TABLE`, 25 `ALTER TABLE`, 0 `user_version` | Migrations are guesswork; a downgrade corrupts. |
| D8 | 25 Store classes, no common base | — | Connection, transaction and locking policy is re-decided 25 times. |
| D9 | Type debt in the core | 70 modules under mypy `ignore_errors` | The least-checked modules are the most central ones. |
| D10 | Tests bound to internals | 89 of 216 test files reach 102 private names. `scripts/private_api_usage.py` splits them: **64** are reachable from an existing public entry point (convert the call), **14** are not (needs a design decision), **24** are module-level helpers (a rename) | The safety net blocks the refactor it should enable. The hard part is 14 names, not 102. |
| D11 | Flat package root | 43 root modules; L0 `secrets.py` and L5 `agent.py` are siblings; `project.py`/`projects.py`, `sessions.py`/`session_facts.py` | Nothing about the file tree tells you the layering. |

## 8. Keeping this true

This file is only useful if it cannot drift. Two mechanisms:

1. **`scripts/import_graph.py` exits non-zero** when there is an import-time cycle or a
   module-level layer violation. Wire it into the `lint` job, or replace it with
   `import-linter` driving the same `LAYERS` table, and the layering in §3 becomes a CI
   gate rather than a description.
2. **The existing ratchets only shrink.** `pyproject.toml` already does this for mypy
   (70 modules) and ruff's per-file ignores — entries are deleted as they are burned down,
   never re-added. Hold new debt to the same rule.

When you change the layering, change `LAYERS` in the script first and let the numbers in
§4 fall out of a re-run. Do not hand-edit them.
