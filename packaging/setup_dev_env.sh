#!/usr/bin/env bash
# One-time dev bootstrap for a fresh checkout: creates the Python venv every
# from-source flow expects at .venv — the browser dev flow runs its
# openworker-server directly, and the Tauri desktop shell falls back to it when
# no packaged sidecar binary is present (src-tauri/src/lib.rs, resolution step 3).
#
# Usage: bash packaging/setup_dev_env.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# uv creates and populates $ROOT/.venv itself — the same path every from-source flow
# expects. --locked installs exactly what uv.lock pins, so this matches CI byte for byte.
# Keep the extras in step with CI (.github/workflows/ci.yml) — the bedrock test suite
# imports boto3 directly, so skipping that extra leaves a fresh env red. The dev group
# (pytest, ruff, mypy) is installed by default; pass --no-dev to skip it.
cd "$ROOT"
uv sync --locked --extra messaging --extra bedrock

"$VENV/bin/python" -c 'import aisuite, coworker' # fail loudly if the wiring broke
echo "Ready: $VENV"
echo "  server: $VENV/bin/openworker-server --cwd /path/to/your/project --port 8765"
