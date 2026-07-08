#!/usr/bin/env bash
# Bootstrap the local repo venv so sibling SDK/adapter packages are importable.
# Run from anywhere inside the Engram repo: bash scripts/setup-python-dev.sh

set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required but not installed" >&2
  exit 1
fi

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

uv sync --extra dev
PYTHON="$ROOT/.venv/bin/python"

uv pip install --python "$PYTHON" -e "$ROOT/sdk/engram-client"
uv pip install --python "$PYTHON" -e "$ROOT/adapters/mcp-server"

"$PYTHON" - <<'PY'
import importlib.util
import sys

missing = [
    name for name in ("engram_client", "engram_mcp")
    if importlib.util.find_spec(name) is None
]
if missing:
    raise SystemExit(f"bootstrap failed; missing imports: {', '.join(missing)}")
print("✓ Local Python dev bootstrap complete: engram_client and engram_mcp are importable")
PY

cat <<'EOF'

Examples:
  .venv/bin/python -m engram_mcp
  .venv/bin/engram-mcp
EOF
