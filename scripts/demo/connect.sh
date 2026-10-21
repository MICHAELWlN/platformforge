#!/bin/bash
# Local loopback supervisor. Does not change a Service type or any cluster object.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
python="${root}/.venv/bin/python"
if [[ ! -x "$python" ]]; then
  echo "missing ${python}" >&2
  exit 1
fi
exec "$python" "${root}/scripts/demo/connect_supervisor.py" "$@"
