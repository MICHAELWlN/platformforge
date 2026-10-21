#!/bin/bash
# Mint a one-hour token for the existing platformforge-mcp-reader ServiceAccount.
# Does not create IAM, a key file in the repo, or a new ServiceAccount.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
CTX="gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
current="$(kubectl config current-context)"
echo "CURRENT=${current}"
if [[ "$current" != "$CTX" ]]; then
  echo "refusing context ${current}" >&2
  exit 2
fi
PYTHONPATH="$root" "$root/.venv/bin/python" - <<'PY'
from pathlib import Path

import yaml

from platformforge.gke import GKE_CONTEXT
from tests.integration.test_mcp_gke import _gke_cluster, _mint_token

path = Path("/tmp/pf-mcp-reader/kubeconfig")
path.parent.mkdir(mode=0o700, exist_ok=True)
server, ca = _gke_cluster()
token = _mint_token()
path.write_text(
    yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": GKE_CONTEXT,
            "clusters": [
                {
                    "name": GKE_CONTEXT,
                    "cluster": {"server": server, "certificate-authority-data": ca},
                }
            ],
            "contexts": [
                {
                    "name": GKE_CONTEXT,
                    "context": {"cluster": GKE_CONTEXT, "user": "platformforge-mcp-reader"},
                }
            ],
            "users": [{"name": "platformforge-mcp-reader", "user": {"token": token}}],
        }
    ),
    encoding="utf-8",
)
path.chmod(0o600)
text = path.read_text(encoding="utf-8")
if "gke-gcloud-auth-plugin" in text or "client-certificate-data" in text:
    raise SystemExit("reader kubeconfig has an operator credential")
print("wrote", path)
print("user platformforge-mcp-reader")
PY
