#!/bin/bash
# stdio entry point for Cursor. Does not open a port and does not mint credentials.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
if [[ -n "${PLATFORMFORGE_MCP_HTTP:-}" ]]; then
  echo "HTTP transport is disabled" >&2
  exit 2
fi
exec "$root/.venv/bin/python" -m platformforge.mcp_server
