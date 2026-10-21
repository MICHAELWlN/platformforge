#!/bin/bash
# Read-only controller check.
set -euo pipefail
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:${PATH}"
CTX="gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
current="$(kubectl config current-context)"
echo "CURRENT=${current}"
if [[ "$current" != "$CTX" ]]; then
  echo "refusing context ${current}" >&2
  exit 2
fi
kubectl --context "$CTX" -n platformforge-dev get deploy canary-controller
kubectl --context "$CTX" -n platformforge-dev logs deploy/canary-controller --tail=3
